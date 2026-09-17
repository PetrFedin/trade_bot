from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from app.domain.trading import Side

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


@dataclass(frozen=True)
class ExecutionCheckpoint:
    checkpoint_id: str
    intent_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_quantity: Decimal
    cumulative_quantity: Decimal
    broker_status: str
    observed_at: datetime
    observed_avg_price: Decimal | None = None

    def validate(self) -> None:
        for name, value in (
            ("checkpoint_id", self.checkpoint_id),
            ("intent_id", self.intent_id),
            ("broker_order_id", self.broker_order_id),
            ("client_order_id", self.client_order_id),
            ("symbol", self.symbol),
            ("broker_status", self.broker_status),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if self.symbol != self.symbol.upper():
            raise ValueError("symbol must be uppercase")
        if not self.order_quantity.is_finite() or self.order_quantity <= 0:
            raise ValueError("order_quantity must be positive and finite")
        if not self.cumulative_quantity.is_finite() or self.cumulative_quantity <= 0:
            raise ValueError("cumulative_quantity must be positive and finite")
        if self.cumulative_quantity > self.order_quantity:
            raise ValueError("cumulative_quantity exceeds order_quantity")
        if self.observed_avg_price is not None and (
            not self.observed_avg_price.is_finite() or self.observed_avg_price <= 0
        ):
            raise ValueError("observed_avg_price must be positive and finite when present")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "intent_id": self.intent_id,
            "broker_order_id": self.broker_order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_quantity": str(self.order_quantity),
            "cumulative_quantity": str(self.cumulative_quantity),
            "broker_status": self.broker_status,
            "observed_avg_price": (
                None if self.observed_avg_price is None else str(self.observed_avg_price)
            ),
        }


def canonical_execution_checkpoint_id(
    *,
    intent_id: str,
    broker_order_id: str,
    cumulative_quantity: Decimal,
    observed_at: datetime,
) -> str:
    if not intent_id.strip() or not broker_order_id.strip():
        raise ValueError("intent_id and broker_order_id are required")
    if not cumulative_quantity.is_finite() or cumulative_quantity <= 0:
        raise ValueError("cumulative_quantity must be positive and finite")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    canonical = "|".join(
        (
            intent_id,
            broker_order_id,
            format(cumulative_quantity.normalize(), "f"),
            observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )
    )
    return "execution-checkpoint:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ExecutionCheckpointStore(Protocol):
    def append(self, checkpoint: ExecutionCheckpoint) -> bool: ...

    def resolve_through(
        self,
        *,
        intent_id: str,
        cumulative_quantity: Decimal,
        occurred_at: datetime,
    ) -> int: ...

    def resolve_from_projected_facts(
        self,
        *,
        intent_id: str,
        occurred_at: datetime,
    ) -> int: ...

    def unresolved(self) -> tuple[ExecutionCheckpoint, ...]: ...

    def unresolved_count(self) -> int: ...


class SQLiteExecutionCheckpointStore:
    """Durable aggregate-execution barrier; it never fabricates exact fills."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
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
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_checkpoint_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    checkpoint_id TEXT NOT NULL
                        REFERENCES execution_checkpoints(checkpoint_id) ON DELETE RESTRICT,
                    state TEXT NOT NULL CHECK (state = 'RESOLVED'),
                    cumulative_quantity TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_execution_checkpoint_events_checkpoint
                    ON execution_checkpoint_events(checkpoint_id, sequence DESC);
                CREATE TRIGGER IF NOT EXISTS execution_checkpoints_no_update
                BEFORE UPDATE ON execution_checkpoints
                BEGIN
                    SELECT RAISE(ABORT, 'execution_checkpoints is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS execution_checkpoints_no_delete
                BEFORE DELETE ON execution_checkpoints
                BEGIN
                    SELECT RAISE(ABORT, 'execution_checkpoints is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS execution_checkpoint_events_no_update
                BEFORE UPDATE ON execution_checkpoint_events
                BEGIN
                    SELECT RAISE(ABORT, 'execution_checkpoint_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS execution_checkpoint_events_no_delete
                BEFORE DELETE ON execution_checkpoint_events
                BEGIN
                    SELECT RAISE(ABORT, 'execution_checkpoint_events is append-only');
                END;
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _canonical_payload(checkpoint: ExecutionCheckpoint) -> str:
        return json.dumps(checkpoint.payload(), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _checkpoint(row: sqlite3.Row) -> ExecutionCheckpoint:
        payload = dict(json.loads(str(row["payload"])))
        avg_price = payload.get("observed_avg_price")
        return ExecutionCheckpoint(
            checkpoint_id=str(row["checkpoint_id"]),
            intent_id=str(payload["intent_id"]),
            broker_order_id=str(payload["broker_order_id"]),
            client_order_id=str(payload["client_order_id"]),
            symbol=str(payload["symbol"]),
            side=Side(str(payload["side"])),
            order_quantity=Decimal(str(payload["order_quantity"])),
            cumulative_quantity=Decimal(str(payload["cumulative_quantity"])),
            broker_status=str(payload["broker_status"]),
            observed_avg_price=None if avg_price is None else Decimal(str(avg_price)),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
        )

    def append(self, checkpoint: ExecutionCheckpoint) -> bool:
        checkpoint.validate()
        observed_at = self._aware(checkpoint.observed_at)
        payload = self._canonical_payload(checkpoint)
        with self._transaction() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO execution_checkpoints
                (checkpoint_id, payload, observed_at) VALUES (?, ?, ?)""",
                (checkpoint.checkpoint_id, payload, observed_at.isoformat()),
            )
            if cursor.rowcount == 1:
                return True
            row = connection.execute(
                """SELECT payload, observed_at FROM execution_checkpoints
                WHERE checkpoint_id=?""",
                (checkpoint.checkpoint_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("execution checkpoint lookup lost conflict")
            if str(row["payload"]) != payload or str(row["observed_at"]) != observed_at.isoformat():
                raise ValueError("EXECUTION_CHECKPOINT_CONFLICT")
            return False

    def unresolved(self) -> tuple[ExecutionCheckpoint, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT c.checkpoint_id, c.payload, c.observed_at
                FROM execution_checkpoints c
                WHERE NOT EXISTS (
                    SELECT 1 FROM execution_checkpoint_events e
                    WHERE e.checkpoint_id=c.checkpoint_id AND e.state='RESOLVED'
                )
                ORDER BY c.observed_at, c.checkpoint_id"""
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._checkpoint(row) for row in rows)

    def unresolved_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT count(*) AS count FROM execution_checkpoints c
                WHERE NOT EXISTS (
                    SELECT 1 FROM execution_checkpoint_events e
                    WHERE e.checkpoint_id=c.checkpoint_id AND e.state='RESOLVED'
                )"""
            ).fetchone()
            return 0 if row is None else int(row["count"])
        finally:
            connection.close()

    def resolve_through(
        self,
        *,
        intent_id: str,
        cumulative_quantity: Decimal,
        occurred_at: datetime,
    ) -> int:
        intent = intent_id.strip()
        if not intent:
            raise ValueError("intent_id is required")
        if not cumulative_quantity.is_finite() or cumulative_quantity <= 0:
            raise ValueError("cumulative_quantity must be positive and finite")
        moment = self._aware(occurred_at)
        resolved = 0
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT c.checkpoint_id, c.payload, c.observed_at
                FROM execution_checkpoints c
                WHERE NOT EXISTS (
                    SELECT 1 FROM execution_checkpoint_events e
                    WHERE e.checkpoint_id=c.checkpoint_id AND e.state='RESOLVED'
                )
                ORDER BY c.observed_at, c.checkpoint_id"""
            ).fetchall()
            for row in rows:
                checkpoint = self._checkpoint(row)
                if checkpoint.intent_id != intent:
                    continue
                if checkpoint.cumulative_quantity > cumulative_quantity:
                    continue
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO execution_checkpoint_events
                    (event_id, checkpoint_id, state, cumulative_quantity, occurred_at)
                    VALUES (?, ?, 'RESOLVED', ?, ?)""",
                    (
                        f"execution-checkpoint-resolved:{checkpoint.checkpoint_id}",
                        checkpoint.checkpoint_id,
                        str(cumulative_quantity),
                        moment.isoformat(),
                    ),
                )
                resolved += int(cursor.rowcount == 1)
        return resolved

    def resolve_from_projected_facts(
        self,
        *,
        intent_id: str,
        occurred_at: datetime,
    ) -> int:
        intent = intent_id.strip()
        if not intent:
            raise ValueError("intent_id is required")
        connection = self._connect()
        try:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not {"execution_facts", "execution_projection_events"}.issubset(tables):
                return 0
            rows = connection.execute(
                """SELECT f.payload FROM execution_facts f
                WHERE EXISTS (
                    SELECT 1 FROM execution_projection_events e
                    WHERE e.execution_fact_id=f.execution_fact_id AND e.state='PROJECTED'
                )"""
            ).fetchall()
        finally:
            connection.close()
        projected: list[Decimal] = []
        for row in rows:
            payload = dict(json.loads(str(row["payload"])))
            if str(payload.get("intent_id", "")) != intent:
                continue
            projected.append(Decimal(str(payload["cumulative_quantity"])))
        if not projected:
            return 0
        return self.resolve_through(
            intent_id=intent,
            cumulative_quantity=max(projected),
            occurred_at=occurred_at,
        )


class PostgresExecutionCheckpointStore:
    """PostgreSQL parity for durable aggregate-execution convergence barriers."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError(
                "install the postgresql extra to use PostgresExecutionCheckpointStore"
            )
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/product/013_execution_checkpoints.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _checkpoint(row: dict[str, object]) -> ExecutionCheckpoint:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise RuntimeError("invalid execution checkpoint payload")
        observed_at = row["observed_at"]
        if not isinstance(observed_at, datetime):
            observed_at = datetime.fromisoformat(str(observed_at))
        avg_price = payload.get("observed_avg_price")
        return ExecutionCheckpoint(
            checkpoint_id=str(row["checkpoint_id"]),
            intent_id=str(payload["intent_id"]),
            broker_order_id=str(payload["broker_order_id"]),
            client_order_id=str(payload["client_order_id"]),
            symbol=str(payload["symbol"]),
            side=Side(str(payload["side"])),
            order_quantity=Decimal(str(payload["order_quantity"])),
            cumulative_quantity=Decimal(str(payload["cumulative_quantity"])),
            broker_status=str(payload["broker_status"]),
            observed_avg_price=None if avg_price is None else Decimal(str(avg_price)),
            observed_at=observed_at,
        )

    def append(self, checkpoint: ExecutionCheckpoint) -> bool:
        checkpoint.validate()
        observed_at = self._aware(checkpoint.observed_at)
        payload = checkpoint.payload()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_execution_checkpoints
                        (checkpoint_id, payload, observed_at)
                        VALUES (%s, %s::jsonb, %s)
                        ON CONFLICT (checkpoint_id) DO NOTHING""",
                        (
                            checkpoint.checkpoint_id,
                            json.dumps(payload, sort_keys=True),
                            observed_at,
                        ),
                    )
                    if cursor.rowcount == 1:
                        return True
                    cursor.execute(
                        """SELECT payload, observed_at FROM astra_execution_checkpoints
                        WHERE checkpoint_id=%s FOR SHARE""",
                        (checkpoint.checkpoint_id,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("execution checkpoint lookup lost conflict")
                    existing_payload = row["payload"]
                    if isinstance(existing_payload, str):
                        existing_payload = json.loads(existing_payload)
                    existing_time = row["observed_at"]
                    if not isinstance(existing_time, datetime):
                        existing_time = datetime.fromisoformat(str(existing_time))
                    if existing_payload != payload or self._aware(existing_time) != observed_at:
                        raise ValueError("EXECUTION_CHECKPOINT_CONFLICT")
                    return False

    def unresolved(self) -> tuple[ExecutionCheckpoint, ...]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT c.checkpoint_id, c.payload, c.observed_at
                    FROM astra_execution_checkpoints c
                    WHERE NOT EXISTS (
                        SELECT 1 FROM astra_execution_checkpoint_events e
                        WHERE e.checkpoint_id=c.checkpoint_id AND e.state='RESOLVED'
                    )
                    ORDER BY c.observed_at, c.checkpoint_id"""
                )
                rows = cursor.fetchall()
        return tuple(self._checkpoint(dict(row)) for row in rows)

    def unresolved_count(self) -> int:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT count(*) AS count FROM astra_execution_checkpoints c
                    WHERE NOT EXISTS (
                        SELECT 1 FROM astra_execution_checkpoint_events e
                        WHERE e.checkpoint_id=c.checkpoint_id AND e.state='RESOLVED'
                    )"""
                )
                row = cursor.fetchone()
                return 0 if row is None else int(row["count"])

    def resolve_through(
        self,
        *,
        intent_id: str,
        cumulative_quantity: Decimal,
        occurred_at: datetime,
    ) -> int:
        intent = intent_id.strip()
        if not intent:
            raise ValueError("intent_id is required")
        if not cumulative_quantity.is_finite() or cumulative_quantity <= 0:
            raise ValueError("cumulative_quantity must be positive and finite")
        moment = self._aware(occurred_at)
        resolved = 0
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT c.checkpoint_id, c.payload, c.observed_at
                        FROM astra_execution_checkpoints c
                        WHERE NOT EXISTS (
                            SELECT 1 FROM astra_execution_checkpoint_events e
                            WHERE e.checkpoint_id=c.checkpoint_id AND e.state='RESOLVED'
                        )
                        ORDER BY c.observed_at, c.checkpoint_id
                        FOR UPDATE OF c"""
                    )
                    rows = cursor.fetchall()
                    for row in rows:
                        checkpoint = self._checkpoint(dict(row))
                        if checkpoint.intent_id != intent:
                            continue
                        if checkpoint.cumulative_quantity > cumulative_quantity:
                            continue
                        cursor.execute(
                            """INSERT INTO astra_execution_checkpoint_events
                            (event_id, checkpoint_id, state, cumulative_quantity, occurred_at)
                            VALUES (%s, %s, 'RESOLVED', %s, %s)
                            ON CONFLICT (event_id) DO NOTHING""",
                            (
                                f"execution-checkpoint-resolved:{checkpoint.checkpoint_id}",
                                checkpoint.checkpoint_id,
                                str(cumulative_quantity),
                                moment,
                            ),
                        )
                        resolved += int(cursor.rowcount == 1)
        return resolved

    def resolve_from_projected_facts(
        self,
        *,
        intent_id: str,
        occurred_at: datetime,
    ) -> int:
        intent = intent_id.strip()
        if not intent:
            raise ValueError("intent_id is required")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT to_regclass('astra_execution_facts') AS facts,
                        to_regclass('astra_execution_projection_events') AS events"""
                )
                tables = cursor.fetchone()
                if tables is None or tables["facts"] is None or tables["events"] is None:
                    return 0
                cursor.execute(
                    """SELECT f.payload FROM astra_execution_facts f
                    WHERE EXISTS (
                        SELECT 1 FROM astra_execution_projection_events e
                        WHERE e.execution_fact_id=f.execution_fact_id
                          AND e.state='PROJECTED'
                    )"""
                )
                rows = cursor.fetchall()
        projected: list[Decimal] = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                raise RuntimeError("invalid execution fact payload")
            if str(payload.get("intent_id", "")) != intent:
                continue
            projected.append(Decimal(str(payload["cumulative_quantity"])))
        if not projected:
            return 0
        return self.resolve_through(
            intent_id=intent,
            cumulative_quantity=max(projected),
            occurred_at=occurred_at,
        )
