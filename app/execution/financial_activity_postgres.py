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
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


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

    def migrate(
        self,
        path: str | Path = "migrations/product/008_financial_activities.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
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
            release_identity=str(row["release_identity"]),
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

    @staticmethod
    def _select_sql(*, for_update: bool = False) -> str:
        suffix = " FOR UPDATE" if for_update else ""
        return (
            "SELECT f.*, p.state, p.reason, p.portfolio_event_id, p.updated_at "
            "FROM astra_financial_activity_facts f "
            "JOIN astra_financial_activity_projection p USING(activity_id) "
            "WHERE f.activity_id=%s" + suffix
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
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_financial_activity_facts
                        (activity_id, activity_type, net_amount, currency, symbol,
                         occurred_at, account_identity, release_identity,
                         source_cursor, payload_hash, canonical_payload, ingested_at)
                        VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s::jsonb, %s
                        )
                        ON CONFLICT (activity_id) DO NOTHING
                        RETURNING activity_id""",
                        (
                            activity.activity_id,
                            activity.activity_type,
                            activity.net_amount,
                            activity.currency,
                            activity.symbol,
                            aware_utc(activity.occurred_at, "occurred_at"),
                            activity.account_identity,
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
                            (activity_id, state, reason, portfolio_event_id, updated_at)
                            VALUES (%s, 'PENDING', NULL, NULL, %s)""",
                            (activity.activity_id, moment),
                        )
                        cursor.execute(
                            self._select_sql(),
                            (activity.activity_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError(
                                "financial activity insert lookup failed"
                            )
                        return self._record(row)

                    cursor.execute(
                        self._select_sql(for_update=True),
                        (activity.activity_id,),
                    )
                    existing = cursor.fetchone()
                    if existing is None:
                        raise RuntimeError(
                            "financial activity conflict row is unavailable"
                        )
                    if str(existing["payload_hash"]) == digest:
                        return self._record(existing)
                    cursor.execute(
                        """INSERT INTO astra_financial_activity_conflicts
                        (activity_id, existing_payload_hash,
                         observed_payload_hash, observed_payload, observed_at)
                        VALUES (%s, %s, %s, %s::jsonb, %s)""",
                        (
                            activity.activity_id,
                            str(existing["payload_hash"]),
                            digest,
                            payload,
                            moment,
                        ),
                    )
                    cursor.execute(
                        """UPDATE astra_financial_activity_projection
                        SET state='QUARANTINED',
                            reason='ACTIVITY_ID_CONFLICT',
                            updated_at=%s
                        WHERE activity_id=%s""",
                        (moment, activity.activity_id),
                    )
                    cursor.execute(
                        self._select_sql(),
                        (activity.activity_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError(
                            "financial activity conflict lookup failed"
                        )
                    return self._record(row)

    def pending(self, *, limit: int = 100) -> tuple[FinancialActivityRecord, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT f.*, p.state, p.reason,
                              p.portfolio_event_id, p.updated_at
                    FROM astra_financial_activity_facts f
                    JOIN astra_financial_activity_projection p USING(activity_id)
                    WHERE p.state='PENDING'
                    ORDER BY f.occurred_at, f.activity_id LIMIT %s""",
                    (limit,),
                )
                rows = cursor.fetchall()
        return tuple(self._record(row) for row in rows)

    def _transition(
        self,
        activity_id: str,
        *,
        state: FinancialProjectionState,
        reason: str | None,
        portfolio_event_id: str | None,
        occurred_at: datetime,
    ) -> FinancialActivityRecord:
        moment = aware_utc(occurred_at, "occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        self._select_sql(for_update=True),
                        (activity_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise KeyError(activity_id)
                    current = FinancialProjectionState(str(row["state"]))
                    if (
                        current is FinancialProjectionState.PROJECTED
                        and state is FinancialProjectionState.PROJECTED
                    ):
                        if str(row["portfolio_event_id"]) != str(portfolio_event_id):
                            raise ValueError("FINANCIAL_PROJECTION_CONFLICT")
                        return self._record(row)
                    if (
                        current is FinancialProjectionState.QUARANTINED
                        and state is not FinancialProjectionState.QUARANTINED
                    ):
                        raise ValueError(
                            "QUARANTINED_FINANCIAL_ACTIVITY_CANNOT_ADVANCE"
                        )
                    cursor.execute(
                        """UPDATE astra_financial_activity_projection
                        SET state=%s, reason=%s,
                            portfolio_event_id=%s, updated_at=%s
                        WHERE activity_id=%s""",
                        (
                            state.value,
                            reason,
                            portfolio_event_id,
                            moment,
                            activity_id,
                        ),
                    )
                    cursor.execute(
                        self._select_sql(),
                        (activity_id,),
                    )
                    updated = cursor.fetchone()
                    if updated is None:
                        raise RuntimeError(
                            "financial projection update lookup failed"
                        )
                    return self._record(updated)

    def mark_projected(
        self,
        activity_id: str,
        *,
        portfolio_event_id: str,
        occurred_at: datetime,
    ) -> FinancialActivityRecord:
        if not portfolio_event_id.strip():
            raise ValueError("portfolio_event_id is required")
        return self._transition(
            activity_id,
            state=FinancialProjectionState.PROJECTED,
            reason=None,
            portfolio_event_id=portfolio_event_id,
            occurred_at=occurred_at,
        )

    def quarantine(
        self,
        activity_id: str,
        *,
        reason: str,
        occurred_at: datetime,
    ) -> FinancialActivityRecord:
        if not reason.strip():
            raise ValueError("quarantine reason is required")
        return self._transition(
            activity_id,
            state=FinancialProjectionState.QUARANTINED,
            reason=reason,
            portfolio_event_id=None,
            occurred_at=occurred_at,
        )

    def _count(self, state: FinancialProjectionState) -> int:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT COUNT(*) AS count
                    FROM astra_financial_activity_projection
                    WHERE state=%s""",
                    (state.value,),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("financial projection count failed")
        return int(row["count"])

    def pending_count(self) -> int:
        return self._count(FinancialProjectionState.PENDING)

    def quarantined_count(self) -> int:
        return self._count(FinancialProjectionState.QUARANTINED)

    def recovery_state(
        self,
        *,
        account_identity: str,
        release_identity: str,
    ) -> FinancialActivityRecoveryState | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT * FROM astra_financial_activity_recovery
                    WHERE account_identity=%s AND release_identity=%s""",
                    (account_identity, release_identity),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        recovered = row["recovered_through"]
        updated = row["updated_at"]
        if not isinstance(recovered, datetime):
            recovered = datetime.fromisoformat(str(recovered))
        if not isinstance(updated, datetime):
            updated = datetime.fromisoformat(str(updated))
        return FinancialActivityRecoveryState(
            account_identity=str(row["account_identity"]),
            release_identity=str(row["release_identity"]),
            recovered_through=aware_utc(recovered, "recovered_through"),
            updated_at=aware_utc(updated, "updated_at"),
        )

    def advance_recovery(
        self,
        *,
        account_identity: str,
        release_identity: str,
        recovered_through: datetime,
        occurred_at: datetime,
    ) -> FinancialActivityRecoveryState:
        if not account_identity.strip() or not release_identity.strip():
            raise ValueError("account_identity and release_identity are required")
        watermark = aware_utc(recovered_through, "recovered_through")
        moment = aware_utc(occurred_at, "occurred_at")
        if watermark > moment:
            raise ValueError("recovered_through cannot exceed occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_financial_activity_recovery
                        (account_identity, release_identity,
                         recovered_through, updated_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (account_identity, release_identity)
                        DO NOTHING
                        RETURNING recovered_through""",
                        (
                            account_identity,
                            release_identity,
                            watermark,
                            moment,
                        ),
                    )
                    inserted = cursor.fetchone() is not None
                    if not inserted:
                        cursor.execute(
                            """SELECT recovered_through
                            FROM astra_financial_activity_recovery
                            WHERE account_identity=%s AND release_identity=%s
                            FOR UPDATE""",
                            (account_identity, release_identity),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError(
                                "financial recovery row unavailable"
                            )
                        existing = row["recovered_through"]
                        if not isinstance(existing, datetime):
                            existing = datetime.fromisoformat(str(existing))
                        if watermark < aware_utc(existing, "recovered_through"):
                            raise ValueError(
                                "FINANCIAL_ACTIVITY_WATERMARK_REGRESSION"
                            )
                        cursor.execute(
                            """UPDATE astra_financial_activity_recovery
                            SET recovered_through=%s, updated_at=%s
                            WHERE account_identity=%s AND release_identity=%s""",
                            (
                                watermark,
                                moment,
                                account_identity,
                                release_identity,
                            ),
                        )
        state = self.recovery_state(
            account_identity=account_identity,
            release_identity=release_identity,
        )
        if state is None:
            raise RuntimeError("financial recovery state persistence failed")
        return state
