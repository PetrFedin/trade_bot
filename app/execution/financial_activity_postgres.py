from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.execution.financial_activity_store import (
    BrokerFinancialActivity,
    FinancialActivityRecord,
    FinancialActivityRecoveryState,
    FinancialProjectionState,
    aware_utc,
    canonical_payload,
    validate_account,
    validate_scope,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


_SELECT_ACTIVITY = """SELECT f.*, p.state, p.reason, p.portfolio_event_id, p.updated_at
FROM astra_financial_activity_facts f
JOIN astra_financial_activity_projection p USING(account_identity, activity_id)
WHERE f.account_identity=%s AND f.activity_id=%s"""

_SELECT_ACTIVITY_FOR_UPDATE = """SELECT f.*, p.state, p.reason,
       p.portfolio_event_id, p.updated_at
FROM astra_financial_activity_facts f
JOIN astra_financial_activity_projection p USING(account_identity, activity_id)
WHERE f.account_identity=%s AND f.activity_id=%s
FOR UPDATE"""


class PostgresFinancialActivityStore:
    """Conflict-aware PostgreSQL financial fact inbox and recovery cursor."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError(
                "install the postgresql extra to use financial activity store"
            )
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            autocommit=False,
        )

    def migrate(self, path: str | Path | None = None) -> None:
        paths = (
            (
                Path("migrations/product/008_financial_activities.sql"),
                Path("migrations/product/014_financial_activity_evidence_fencing.sql"),
            )
            if path is None
            else (Path(path),)
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                for migration_path in paths:
                    cursor.execute(migration_path.read_text(encoding="utf-8"))
            connection.commit()

    @staticmethod
    def _record(row: Mapping[str, object]) -> FinancialActivityRecord:
        occurred_at = row["occurred_at"]
        updated_at = row["updated_at"]
        if not isinstance(occurred_at, datetime):
            occurred_at = datetime.fromisoformat(str(occurred_at))
        if not isinstance(updated_at, datetime):
            updated_at = datetime.fromisoformat(str(updated_at))
        payload = row["canonical_payload"]
        if not isinstance(payload, str):
            payload = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )
        activity = BrokerFinancialActivity(
            activity_id=str(row["activity_id"]),
            activity_type=str(row["activity_type"]),
            net_amount=Decimal(str(row["net_amount"])),
            currency=str(row["currency"]),
            symbol=None if row["symbol"] is None else str(row["symbol"]),
            occurred_at=aware_utc(occurred_at, "occurred_at"),
            account_identity=str(row["account_identity"]),
            release_identity=str(row["first_seen_release_identity"]),
            source_cursor=str(row["source_cursor"]),
            canonical_payload=canonical_payload(payload),
        )
        activity.validate()
        return FinancialActivityRecord(
            activity=activity,
            state=FinancialProjectionState(str(row["state"])),
            reason=None if row["reason"] is None else str(row["reason"]),
            portfolio_event_id=(
                None
                if row["portfolio_event_id"] is None
                else str(row["portfolio_event_id"])
            ),
            updated_at=aware_utc(updated_at, "updated_at"),
        )

    def ingest(
        self,
        activity: BrokerFinancialActivity,
        *,
        ingested_at: datetime,
    ) -> FinancialActivityRecord:
        activity.validate()
        moment = aware_utc(ingested_at, "ingested_at")
        payload = canonical_payload(activity.canonical_payload)
        digest = activity.payload_hash
        identity = (activity.account_identity, activity.activity_id)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_financial_activity_facts
                        (account_identity, activity_id, activity_type, net_amount,
                         currency, symbol, occurred_at,
                         first_seen_release_identity, source_cursor, payload_hash,
                         canonical_payload, ingested_at)
                        VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s::jsonb, %s
                        )
                        ON CONFLICT (account_identity, activity_id) DO NOTHING
                        RETURNING activity_id""",
                        (
                            activity.account_identity,
                            activity.activity_id,
                            activity.activity_type,
                            activity.net_amount,
                            activity.currency,
                            activity.symbol,
                            aware_utc(activity.occurred_at, "occurred_at"),
                            activity.release_identity,
                            activity.source_cursor,
                            digest,
                            payload,
                            moment,
                        ),
                    )
                    inserted = cursor.fetchone() is not None
                    if inserted:
                        cursor.execute(
                            """INSERT INTO astra_financial_activity_projection
                            (account_identity, activity_id, state, reason,
                             portfolio_event_id, updated_at)
                            VALUES (%s, %s, 'PENDING', NULL, NULL, %s)""",
                            (*identity, moment),
                        )
                        cursor.execute(_SELECT_ACTIVITY, identity)
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError(
                                "financial activity insert lookup failed"
                            )
                        return self._record(row)

                    cursor.execute(_SELECT_ACTIVITY_FOR_UPDATE, identity)
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("financial activity conflict lookup failed")
                    existing = self._record(row)
                    if existing.activity.payload_hash == digest:
                        return existing

                    conflict_event_id = (
                        f"financial-activity-conflict:{activity.account_identity}:"
                        f"{activity.activity_id}:{digest}"
                    )
                    cursor.execute(
                        """INSERT INTO astra_financial_activity_conflicts
                        (conflict_event_id, account_identity, activity_id,
                         existing_payload_hash, conflicting_payload_hash,
                         conflicting_payload, observed_at)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                        ON CONFLICT (conflict_event_id) DO NOTHING""",
                        (
                            conflict_event_id,
                            activity.account_identity,
                            activity.activity_id,
                            existing.activity.payload_hash,
                            digest,
                            payload,
                            moment,
                        ),
                    )
                    cursor.execute(
                        """UPDATE astra_financial_activity_projection
                        SET state='QUARANTINED', reason='ACTIVITY_ID_CONFLICT',
                            portfolio_event_id=NULL, updated_at=%s
                        WHERE account_identity=%s AND activity_id=%s""",
                        (moment, *identity),
                    )
                    cursor.execute(_SELECT_ACTIVITY, identity)
                    conflicted = cursor.fetchone()
                    if conflicted is None:
                        raise RuntimeError("financial activity quarantine lookup failed")
                    return self._record(conflicted)

    def get(
        self,
        account_identity: str,
        activity_id: str,
    ) -> FinancialActivityRecord | None:
        account = validate_account(account_identity)
        activity = activity_id.strip()
        if not activity:
            raise ValueError("activity_id is required")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(_SELECT_ACTIVITY, (account, activity))
                row = cursor.fetchone()
        return None if row is None else self._record(row)

    def pending(
        self,
        *,
        account_identity: str,
        limit: int = 100,
    ) -> tuple[FinancialActivityRecord, ...]:
        account = validate_account(account_identity)
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT f.*, p.state, p.reason, p.portfolio_event_id,
                              p.updated_at
                    FROM astra_financial_activity_facts f
                    JOIN astra_financial_activity_projection p
                      USING(account_identity, activity_id)
                    WHERE f.account_identity=%s AND p.state='PENDING'
                    ORDER BY f.occurred_at, f.activity_id
                    LIMIT %s""",
                    (account, limit),
                )
                rows = cursor.fetchall()
        return tuple(self._record(row) for row in rows)

    def unresolved_count(self, *, account_identity: str) -> int:
        account = validate_account(account_identity)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT count(*) AS unresolved
                    FROM astra_financial_activity_projection
                    WHERE account_identity=%s AND state!='PROJECTED'""",
                    (account,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("financial activity count lookup failed")
        return int(row["unresolved"])

    def quarantined_count(self, *, account_identity: str) -> int:
        account = validate_account(account_identity)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT count(*) AS quarantined
                    FROM astra_financial_activity_projection
                    WHERE account_identity=%s AND state='QUARANTINED'""",
                    (account,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("financial activity count lookup failed")
        return int(row["quarantined"])

    def projection_counts(self, *, account_identity: str) -> dict[str, int]:
        account = validate_account(account_identity)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT state, count(*) AS count
                    FROM astra_financial_activity_projection
                    WHERE account_identity=%s GROUP BY state""",
                    (account,),
                )
                rows = cursor.fetchall()
        result = {
            FinancialProjectionState.PENDING.value: 0,
            FinancialProjectionState.PROJECTED.value: 0,
            FinancialProjectionState.QUARANTINED.value: 0,
        }
        for row in rows:
            result[str(row["state"])] = int(row["count"])
        return result

    def mark_projected(
        self,
        *,
        account_identity: str,
        activity_id: str,
        portfolio_event_id: str,
        occurred_at: datetime,
    ) -> FinancialActivityRecord:
        account = validate_account(account_identity)
        activity = activity_id.strip()
        event_id = portfolio_event_id.strip()
        if not activity or not event_id:
            raise ValueError("activity_id and portfolio_event_id are required")
        moment = aware_utc(occurred_at, "occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(_SELECT_ACTIVITY_FOR_UPDATE, (account, activity))
                    row = cursor.fetchone()
                    if row is None:
                        raise KeyError(activity)
                    current = self._record(row)
                    if current.state is FinancialProjectionState.QUARANTINED:
                        raise ValueError("QUARANTINED_ACTIVITY_CANNOT_PROJECT")
                    if current.state is FinancialProjectionState.PROJECTED:
                        if current.portfolio_event_id != event_id:
                            raise ValueError("PORTFOLIO_EVENT_ID_CONFLICT")
                        return current
                    cursor.execute(
                        """UPDATE astra_financial_activity_projection
                        SET state='PROJECTED', reason=NULL,
                            portfolio_event_id=%s, updated_at=%s
                        WHERE account_identity=%s AND activity_id=%s""",
                        (event_id, moment, account, activity),
                    )
                    cursor.execute(_SELECT_ACTIVITY, (account, activity))
                    projected = cursor.fetchone()
                    if projected is None:
                        raise RuntimeError("financial activity projection lookup failed")
                    return self._record(projected)

    def mark_quarantined(
        self,
        *,
        account_identity: str,
        activity_id: str,
        reason: str,
        occurred_at: datetime,
    ) -> FinancialActivityRecord:
        account = validate_account(account_identity)
        activity = activity_id.strip()
        normalized_reason = reason.strip()
        if not activity or not normalized_reason:
            raise ValueError("activity_id and reason are required")
        moment = aware_utc(occurred_at, "occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(_SELECT_ACTIVITY_FOR_UPDATE, (account, activity))
                    row = cursor.fetchone()
                    if row is None:
                        raise KeyError(activity)
                    current = self._record(row)
                    if current.state is FinancialProjectionState.PROJECTED:
                        raise ValueError("PROJECTED_ACTIVITY_CANNOT_QUARANTINE")
                    if (
                        current.state is FinancialProjectionState.QUARANTINED
                        and current.reason == normalized_reason
                    ):
                        return current
                    cursor.execute(
                        """UPDATE astra_financial_activity_projection
                        SET state='QUARANTINED', reason=%s,
                            portfolio_event_id=NULL, updated_at=%s
                        WHERE account_identity=%s AND activity_id=%s""",
                        (normalized_reason, moment, account, activity),
                    )
                    cursor.execute(_SELECT_ACTIVITY, (account, activity))
                    quarantined = cursor.fetchone()
                    if quarantined is None:
                        raise RuntimeError("financial activity quarantine lookup failed")
                    return self._record(quarantined)

    def recovery_state(
        self,
        *,
        account_identity: str,
        release_identity: str,
    ) -> FinancialActivityRecoveryState | None:
        account, release = validate_scope(account_identity, release_identity)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT account_identity, release_identity,
                              safe_cursor, safe_observed_at,
                              last_recovery_started_at, last_recovery_completed_at,
                              last_recovery_complete, last_error,
                              updated_at
                    FROM astra_financial_activity_recovery
                    WHERE account_identity=%s AND release_identity=%s""",
                    (account, release),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        return FinancialActivityRecoveryState(
            account_identity=str(row["account_identity"]),
            release_identity=str(row["release_identity"]),
            safe_cursor=str(row["safe_cursor"]),
            safe_observed_at=aware_utc(
                row["safe_observed_at"],
                "safe_observed_at",
            ),
            last_recovery_started_at=aware_utc(
                row["last_recovery_started_at"],
                "last_recovery_started_at",
            ),
            last_recovery_completed_at=(
                None
                if row["last_recovery_completed_at"] is None
                else aware_utc(
                    row["last_recovery_completed_at"],
                    "last_recovery_completed_at",
                )
            ),
            last_recovery_complete=bool(row["last_recovery_complete"]),
            last_error=None if row["last_error"] is None else str(row["last_error"]),
            updated_at=aware_utc(row["updated_at"], "updated_at"),
        )

    def mark_recovery_started(
        self,
        *,
        account_identity: str,
        release_identity: str,
        occurred_at: datetime,
    ) -> FinancialActivityRecoveryState:
        account, release = validate_scope(account_identity, release_identity)
        moment = aware_utc(occurred_at, "occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_financial_activity_recovery
                        (account_identity, release_identity,
                         safe_cursor, safe_observed_at,
                         last_recovery_started_at, last_recovery_completed_at,
                         last_recovery_complete, last_error, updated_at)
                        VALUES (%s, %s, '', %s, %s, NULL, FALSE, NULL, %s)
                        ON CONFLICT (account_identity, release_identity) DO UPDATE SET
                            last_recovery_started_at=EXCLUDED.last_recovery_started_at,
                            last_recovery_complete=FALSE,
                            last_error=NULL,
                            updated_at=EXCLUDED.updated_at""",
                        (account, release, moment, moment, moment),
                    )
        state = self.recovery_state(
            account_identity=account,
            release_identity=release,
        )
        if state is None:
            raise RuntimeError("financial recovery start persistence failed")
        return state

    def mark_recovery_completed(
        self,
        *,
        account_identity: str,
        release_identity: str,
        safe_cursor: str,
        safe_observed_at: datetime,
        occurred_at: datetime,
    ) -> FinancialActivityRecoveryState:
        account, release = validate_scope(account_identity, release_identity)
        cursor = safe_cursor.strip()
        if not cursor:
            raise ValueError("safe_cursor is required")
        observed = aware_utc(safe_observed_at, "safe_observed_at")
        moment = aware_utc(occurred_at, "occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as db_cursor:
                    db_cursor.execute(
                        """SELECT safe_observed_at, safe_cursor
                        FROM astra_financial_activity_recovery
                        WHERE account_identity=%s AND release_identity=%s
                        FOR UPDATE""",
                        (account, release),
                    )
                    row = db_cursor.fetchone()
                    if row is not None:
                        existing_observed = aware_utc(
                            row["safe_observed_at"],
                            "safe_observed_at",
                        )
                        existing_cursor = str(row["safe_cursor"])
                        if (observed, cursor) < (existing_observed, existing_cursor):
                            raise ValueError("RECOVERY_CURSOR_REGRESSION")
                    db_cursor.execute(
                        """INSERT INTO astra_financial_activity_recovery
                        (account_identity, release_identity,
                         safe_cursor, safe_observed_at,
                         last_recovery_started_at, last_recovery_completed_at,
                         last_recovery_complete, last_error, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE, NULL, %s)
                        ON CONFLICT (account_identity, release_identity) DO UPDATE SET
                            safe_cursor=EXCLUDED.safe_cursor,
                            safe_observed_at=EXCLUDED.safe_observed_at,
                            last_recovery_completed_at=EXCLUDED.last_recovery_completed_at,
                            last_recovery_complete=TRUE,
                            last_error=NULL,
                            updated_at=EXCLUDED.updated_at""",
                        (account, release, cursor, observed, moment, moment, moment),
                    )
        state = self.recovery_state(
            account_identity=account,
            release_identity=release,
        )
        if state is None:
            raise RuntimeError("financial recovery completion persistence failed")
        return state

    def mark_recovery_failed(
        self,
        *,
        account_identity: str,
        release_identity: str,
        error: str,
        occurred_at: datetime,
    ) -> FinancialActivityRecoveryState:
        account, release = validate_scope(account_identity, release_identity)
        message = error.strip()
        if not message:
            raise ValueError("error is required")
        moment = aware_utc(occurred_at, "occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_financial_activity_recovery
                        (account_identity, release_identity,
                         safe_cursor, safe_observed_at,
                         last_recovery_started_at, last_recovery_completed_at,
                         last_recovery_complete, last_error, updated_at)
                        VALUES (%s, %s, '', %s, %s, NULL, FALSE, %s, %s)
                        ON CONFLICT (account_identity, release_identity) DO UPDATE SET
                            last_recovery_complete=FALSE,
                            last_error=EXCLUDED.last_error,
                            updated_at=EXCLUDED.updated_at""",
                        (account, release, moment, moment, message, moment),
                    )
        state = self.recovery_state(
            account_identity=account,
            release_identity=release,
        )
        if state is None:
            raise RuntimeError("financial recovery failure persistence failed")
        return state

    def reset_for_test(self) -> None:
        """Delete integration-test state; never use this from runtime code."""

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """TRUNCATE TABLE
                    astra_financial_activity_conflicts,
                    astra_financial_activity_recovery,
                    astra_financial_activity_projection,
                    astra_financial_activity_facts
                    RESTART IDENTITY CASCADE"""
                )
            connection.commit()
