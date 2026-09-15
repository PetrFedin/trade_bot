from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Mapping, Protocol

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class FinancialProjectionState(StrEnum):
    PENDING = "PENDING"
    PROJECTED = "PROJECTED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class BrokerFinancialActivity:
    activity_id: str
    activity_type: str
    net_amount: Decimal
    currency: str
    occurred_at: datetime
    account_identity: str
    release_identity: str
    source_cursor: str
    canonical_payload: str
    symbol: str | None = None

    def validate(self) -> None:
        for name, value in (
            ("activity_id", self.activity_id),
            ("activity_type", self.activity_type),
            ("currency", self.currency),
            ("account_identity", self.account_identity),
            ("release_identity", self.release_identity),
            ("source_cursor", self.source_cursor),
            ("canonical_payload", self.canonical_payload),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if self.activity_type != self.activity_type.upper():
            raise ValueError("activity_type must be uppercase")
        if self.currency != self.currency.upper():
            raise ValueError("currency must be uppercase")
        if self.symbol is not None and (
            not self.symbol or self.symbol != self.symbol.upper()
        ):
            raise ValueError("symbol must be uppercase when supplied")
        if not self.net_amount.is_finite():
            raise ValueError("net_amount must be finite")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        try:
            payload = json.loads(self.canonical_payload)
        except json.JSONDecodeError as exc:
            raise ValueError("canonical_payload must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("canonical_payload must be a JSON object")

    @property
    def payload_hash(self) -> str:
        self.validate()
        envelope = json.dumps(
            {
                "account_identity": self.account_identity,
                "release_identity": self.release_identity,
                "payload": json.loads(self.canonical_payload),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(envelope.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FinancialActivityRecord:
    activity: BrokerFinancialActivity
    state: FinancialProjectionState
    reason: str | None
    portfolio_event_id: str | None
    updated_at: datetime


@dataclass(frozen=True)
class FinancialActivityRecoveryState:
    account_identity: str
    release_identity: str
    recovered_through: datetime
    updated_at: datetime


class FinancialActivityStore(Protocol):
    def ingest(
        self,
        activity: BrokerFinancialActivity,
        *,
        ingested_at: datetime,
    ) -> FinancialActivityRecord: ...

    def pending(self, *, limit: int = 100) -> tuple[FinancialActivityRecord, ...]: ...

    def mark_projected(
        self,
        activity_id: str,
        *,
        portfolio_event_id: str,
        occurred_at: datetime,
    ) -> FinancialActivityRecord: ...

    def quarantine(
        self,
        activity_id: str,
        *,
        reason: str,
        occurred_at: datetime,
    ) -> FinancialActivityRecord: ...

    def pending_count(self) -> int: ...

    def quarantined_count(self) -> int: ...

    def recovery_state(
        self,
        *,
        account_identity: str,
        release_identity: str,
    ) -> FinancialActivityRecoveryState | None: ...

    def advance_recovery(
        self,
        *,
        account_identity: str,
        release_identity: str,
        recovered_through: datetime,
        occurred_at: datetime,
    ) -> FinancialActivityRecoveryState: ...


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_payload(value: str) -> str:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("canonical payload must be an object")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class SQLiteFinancialActivityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS financial_activity_facts (
                    activity_id TEXT PRIMARY KEY,
                    activity_type TEXT NOT NULL,
                    net_amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    symbol TEXT,
                    occurred_at TEXT NOT NULL,
                    account_identity TEXT NOT NULL,
                    release_identity TEXT NOT NULL,
                    source_cursor TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    canonical_payload TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS financial_activity_projection (
                    activity_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    reason TEXT,
                    portfolio_event_id TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(activity_id) REFERENCES financial_activity_facts(activity_id)
                );
                CREATE TABLE IF NOT EXISTS financial_activity_conflicts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity_id TEXT NOT NULL,
                    existing_payload_hash TEXT NOT NULL,
                    observed_payload_hash TEXT NOT NULL,
                    observed_payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS financial_activity_recovery (
                    account_identity TEXT NOT NULL,
                    release_identity TEXT NOT NULL,
                    recovered_through TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_identity, release_identity)
                );
                CREATE INDEX IF NOT EXISTS idx_financial_projection_state
                ON financial_activity_projection(state, updated_at, activity_id);
                CREATE TRIGGER IF NOT EXISTS financial_activity_facts_no_update
                BEFORE UPDATE ON financial_activity_facts BEGIN
                    SELECT RAISE(ABORT, 'financial_activity_facts is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS financial_activity_facts_no_delete
                BEFORE DELETE ON financial_activity_facts BEGIN
                    SELECT RAISE(ABORT, 'financial_activity_facts is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS financial_activity_conflicts_no_update
                BEFORE UPDATE ON financial_activity_conflicts BEGIN
                    SELECT RAISE(ABORT, 'financial_activity_conflicts is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS financial_activity_conflicts_no_delete
                BEFORE DELETE ON financial_activity_conflicts BEGIN
                    SELECT RAISE(ABORT, 'financial_activity_conflicts is append-only');
                END;
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> FinancialActivityRecord:
        activity = BrokerFinancialActivity(
            activity_id=str(row["activity_id"]),
            activity_type=str(row["activity_type"]),
            net_amount=Decimal(str(row["net_amount"])),
            currency=str(row["currency"]),
            symbol=None if row["symbol"] is None else str(row["symbol"]),
            occurred_at=_aware(datetime.fromisoformat(str(row["occurred_at"])), "occurred_at"),
            account_identity=str(row["account_identity"]),
            release_identity=str(row["release_identity"]),
            source_cursor=str(row["source_cursor"]),
            canonical_payload=str(row["canonical_payload"]),
        )
        activity.validate()
        return FinancialActivityRecord(
            activity=activity,
            state=FinancialProjectionState(str(row["state"])),
            reason=None if row["reason"] is None else str(row["reason"]),
            portfolio_event_id=(
                None if row["portfolio_event_id"] is None else str(row["portfolio_event_id"])
            ),
            updated_at=_aware(datetime.fromisoformat(str(row["updated_at"])), "updated_at"),
        )

    def _select(self, connection: sqlite3.Connection, activity_id: str) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT f.*, p.state, p.reason, p.portfolio_event_id, p.updated_at
            FROM financial_activity_facts f
            JOIN financial_activity_projection p USING(activity_id)
            WHERE f.activity_id=?""",
            (activity_id,),
        ).fetchone()

    def ingest(
        self,
        activity: BrokerFinancialActivity,
        *,
        ingested_at: datetime,
    ) -> FinancialActivityRecord:
        activity.validate()
        moment = _aware(ingested_at, "ingested_at")
        payload = _canonical_payload(activity.canonical_payload)
        digest = activity.payload_hash
        with self._transaction() as connection:
            existing = self._select(connection, activity.activity_id)
            if existing is not None:
                if str(existing["payload_hash"]) == digest:
                    return self._record(existing)
                connection.execute(
                    """INSERT INTO financial_activity_conflicts
                    (activity_id, existing_payload_hash, observed_payload_hash,
                     observed_payload, observed_at) VALUES (?, ?, ?, ?, ?)""",
                    (
                        activity.activity_id,
                        str(existing["payload_hash"]),
                        digest,
                        payload,
                        moment.isoformat(),
                    ),
                )
                connection.execute(
                    """UPDATE financial_activity_projection
                    SET state='QUARANTINED', reason='ACTIVITY_ID_CONFLICT', updated_at=?
                    WHERE activity_id=?""",
                    (moment.isoformat(), activity.activity_id),
                )
                row = self._select(connection, activity.activity_id)
                if row is None:
                    raise RuntimeError("financial activity conflict lookup failed")
                return self._record(row)

            connection.execute(
                """INSERT INTO financial_activity_facts
                (activity_id, activity_type, net_amount, currency, symbol, occurred_at,
                 account_identity, release_identity, source_cursor, payload_hash,
                 canonical_payload, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    activity.activity_id,
                    activity.activity_type,
                    str(activity.net_amount),
                    activity.currency,
                    activity.symbol,
                    _aware(activity.occurred_at, "occurred_at").isoformat(),
                    activity.account_identity,
                    activity.release_identity,
                    activity.source_cursor,
                    digest,
                    payload,
                    moment.isoformat(),
                ),
            )
            connection.execute(
                """INSERT INTO financial_activity_projection
                (activity_id, state, reason, portfolio_event_id, updated_at)
                VALUES (?, 'PENDING', NULL, NULL, ?)""",
                (activity.activity_id, moment.isoformat()),
            )
            row = self._select(connection, activity.activity_id)
            if row is None:
                raise RuntimeError("financial activity insert lookup failed")
            return self._record(row)

    def pending(self, *, limit: int = 100) -> tuple[FinancialActivityRecord, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT f.*, p.state, p.reason, p.portfolio_event_id, p.updated_at
                FROM financial_activity_facts f
                JOIN financial_activity_projection p USING(activity_id)
                WHERE p.state='PENDING'
                ORDER BY f.occurred_at, f.activity_id LIMIT ?""",
                (limit,),
            ).fetchall()
            return tuple(self._record(row) for row in rows)
        finally:
            connection.close()

    def _transition(
        self,
        activity_id: str,
        *,
        state: FinancialProjectionState,
        reason: str | None,
        portfolio_event_id: str | None,
        occurred_at: datetime,
    ) -> FinancialActivityRecord:
        moment = _aware(occurred_at, "occurred_at")
        with self._transaction() as connection:
            row = self._select(connection, activity_id)
            if row is None:
                raise KeyError(activity_id)
            current = FinancialProjectionState(str(row["state"]))
            if current is FinancialProjectionState.PROJECTED and state is FinancialProjectionState.PROJECTED:
                if str(row["portfolio_event_id"]) != str(portfolio_event_id):
                    raise ValueError("FINANCIAL_PROJECTION_CONFLICT")
                return self._record(row)
            if current is FinancialProjectionState.QUARANTINED and state is not FinancialProjectionState.QUARANTINED:
                raise ValueError("QUARANTINED_FINANCIAL_ACTIVITY_CANNOT_ADVANCE")
            connection.execute(
                """UPDATE financial_activity_projection
                SET state=?, reason=?, portfolio_event_id=?, updated_at=? WHERE activity_id=?""",
                (state.value, reason, portfolio_event_id, moment.isoformat(), activity_id),
            )
            updated = self._select(connection, activity_id)
            if updated is None:
                raise RuntimeError("financial projection update lookup failed")
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
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM financial_activity_projection WHERE state=?",
                (state.value,),
            ).fetchone()
            return int(row["count"])
        finally:
            connection.close()

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
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT * FROM financial_activity_recovery
                WHERE account_identity=? AND release_identity=?""",
                (account_identity, release_identity),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return FinancialActivityRecoveryState(
            account_identity=str(row["account_identity"]),
            release_identity=str(row["release_identity"]),
            recovered_through=_aware(
                datetime.fromisoformat(str(row["recovered_through"])), "recovered_through"
            ),
            updated_at=_aware(datetime.fromisoformat(str(row["updated_at"])), "updated_at"),
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
        watermark = _aware(recovered_through, "recovered_through")
        moment = _aware(occurred_at, "occurred_at")
        if watermark > moment:
            raise ValueError("recovered_through cannot exceed occurred_at")
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT recovered_through FROM financial_activity_recovery
                WHERE account_identity=? AND release_identity=?""",
                (account_identity, release_identity),
            ).fetchone()
            if row is not None:
                existing = _aware(
                    datetime.fromisoformat(str(row["recovered_through"])),
                    "recovered_through",
                )
                if watermark < existing:
                    raise ValueError("FINANCIAL_ACTIVITY_WATERMARK_REGRESSION")
            connection.execute(
                """INSERT INTO financial_activity_recovery
                (account_identity, release_identity, recovered_through, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_identity, release_identity) DO UPDATE SET
                    recovered_through=excluded.recovered_through,
                    updated_at=excluded.updated_at""",
                (
                    account_identity,
                    release_identity,
                    watermark.isoformat(),
                    moment.isoformat(),
                ),
            )
        state = self.recovery_state(
            account_identity=account_identity,
            release_identity=release_identity,
        )
        if state is None:
            raise RuntimeError("financial recovery state persistence failed")
        return state


class PostgresFinancialActivityStore:
    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use financial activity store")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(self, path: str | Path = "migrations/product/008_financial_activities.sql") -> None:
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
            payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        activity = BrokerFinancialActivity(
            activity_id=str(row["activity_id"]),
            activity_type=str(row["activity_type"]),
            net_amount=Decimal(str(row["net_amount"])),
            currency=str(row["currency"]),
            symbol=None if row["symbol"] is None else str(row["symbol"]),
            occurred_at=_aware(occurred_at, "occurred_at"),
            account_identity=str(row["account_identity"]),
            release_identity=str(row["release_identity"]),
            source_cursor=str(row["source_cursor"]),
            canonical_payload=_canonical_payload(payload),
        )
        return FinancialActivityRecord(
            activity=activity,
            state=FinancialProjectionState(str(row["state"])),
            reason=None if row["reason"] is None else str(row["reason"]),
            portfolio_event_id=(
                None if row["portfolio_event_id"] is None else str(row["portfolio_event_id"])
            ),
            updated_at=_aware(updated_at, "updated_at"),
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
        moment = _aware(ingested_at, "ingested_at")
        payload = _canonical_payload(activity.canonical_payload)
        digest = activity.payload_hash
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(self._select_sql(for_update=True), (activity.activity_id,))
                    existing = cursor.fetchone()
                    if existing is not None:
                        if str(existing["payload_hash"]) == digest:
                            return self._record(existing)
                        cursor.execute(
                            """INSERT INTO astra_financial_activity_conflicts
                            (activity_id, existing_payload_hash, observed_payload_hash,
                             observed_payload, observed_at) VALUES (%s, %s, %s, %s::jsonb, %s)""",
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
                            SET state='QUARANTINED', reason='ACTIVITY_ID_CONFLICT', updated_at=%s
                            WHERE activity_id=%s""",
                            (moment, activity.activity_id),
                        )
                        cursor.execute(self._select_sql(), (activity.activity_id,))
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("financial activity conflict lookup failed")
                        return self._record(row)

                    cursor.execute(
                        """INSERT INTO astra_financial_activity_facts
                        (activity_id, activity_type, net_amount, currency, symbol, occurred_at,
                         account_identity, release_identity, source_cursor, payload_hash,
                         canonical_payload, ingested_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)""",
                        (
                            activity.activity_id,
                            activity.activity_type,
                            activity.net_amount,
                            activity.currency,
                            activity.symbol,
                            _aware(activity.occurred_at, "occurred_at"),
                            activity.account_identity,
                            activity.release_identity,
                            activity.source_cursor,
                            digest,
                            payload,
                            moment,
                        ),
                    )
                    cursor.execute(
                        """INSERT INTO astra_financial_activity_projection
                        (activity_id, state, reason, portfolio_event_id, updated_at)
                        VALUES (%s, 'PENDING', NULL, NULL, %s)""",
                        (activity.activity_id, moment),
                    )
                    cursor.execute(self._select_sql(), (activity.activity_id,))
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("financial activity insert lookup failed")
                    return self._record(row)

    def pending(self, *, limit: int = 100) -> tuple[FinancialActivityRecord, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT f.*, p.state, p.reason, p.portfolio_event_id, p.updated_at
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
        moment = _aware(occurred_at, "occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(self._select_sql(for_update=True), (activity_id,))
                    row = cursor.fetchone()
                    if row is None:
                        raise KeyError(activity_id)
                    current = FinancialProjectionState(str(row["state"]))
                    if current is FinancialProjectionState.PROJECTED and state is FinancialProjectionState.PROJECTED:
                        if str(row["portfolio_event_id"]) != str(portfolio_event_id):
                            raise ValueError("FINANCIAL_PROJECTION_CONFLICT")
                        return self._record(row)
                    if current is FinancialProjectionState.QUARANTINED and state is not FinancialProjectionState.QUARANTINED:
                        raise ValueError("QUARANTINED_FINANCIAL_ACTIVITY_CANNOT_ADVANCE")
                    cursor.execute(
                        """UPDATE astra_financial_activity_projection
                        SET state=%s, reason=%s, portfolio_event_id=%s, updated_at=%s
                        WHERE activity_id=%s""",
                        (state.value, reason, portfolio_event_id, moment, activity_id),
                    )
                    cursor.execute(self._select_sql(), (activity_id,))
                    updated = cursor.fetchone()
                    if updated is None:
                        raise RuntimeError("financial projection update lookup failed")
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
                    "SELECT COUNT(*) AS count FROM astra_financial_activity_projection WHERE state=%s",
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
            recovered_through=_aware(recovered, "recovered_through"),
            updated_at=_aware(updated, "updated_at"),
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
        watermark = _aware(recovered_through, "recovered_through")
        moment = _aware(occurred_at, "occurred_at")
        if watermark > moment:
            raise ValueError("recovered_through cannot exceed occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT recovered_through FROM astra_financial_activity_recovery
                        WHERE account_identity=%s AND release_identity=%s FOR UPDATE""",
                        (account_identity, release_identity),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        existing = row["recovered_through"]
                        if not isinstance(existing, datetime):
                            existing = datetime.fromisoformat(str(existing))
                        if watermark < _aware(existing, "recovered_through"):
                            raise ValueError("FINANCIAL_ACTIVITY_WATERMARK_REGRESSION")
                    cursor.execute(
                        """INSERT INTO astra_financial_activity_recovery
                        (account_identity, release_identity, recovered_through, updated_at)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (account_identity, release_identity) DO UPDATE SET
                            recovered_through=EXCLUDED.recovered_through,
                            updated_at=EXCLUDED.updated_at""",
                        (account_identity, release_identity, watermark, moment),
                    )
        state = self.recovery_state(
            account_identity=account_identity,
            release_identity=release_identity,
        )
        if state is None:
            raise RuntimeError("financial recovery state persistence failed")
        return state
