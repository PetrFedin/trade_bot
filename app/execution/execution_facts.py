from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.domain.trading import Fill, Side

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class ExecutionProjectionState(StrEnum):
    PENDING = "PENDING"
    QUARANTINED = "QUARANTINED"
    PROJECTED = "PROJECTED"


@dataclass(frozen=True)
class ExecutionFact:
    execution_fact_id: str
    intent_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_quantity: Decimal
    cumulative_quantity: Decimal
    quantity: Decimal
    price: Decimal
    fee: Decimal
    occurred_at: datetime

    def validate(self) -> None:
        for name, value in (
            ("execution_fact_id", self.execution_fact_id),
            ("intent_id", self.intent_id),
            ("broker_order_id", self.broker_order_id),
            ("client_order_id", self.client_order_id),
            ("symbol", self.symbol),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if self.symbol != self.symbol.upper():
            raise ValueError("symbol must be uppercase")
        for name, value in (
            ("order_quantity", self.order_quantity),
            ("cumulative_quantity", self.cumulative_quantity),
            ("quantity", self.quantity),
            ("price", self.price),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not self.fee.is_finite() or self.fee < 0:
            raise ValueError("fee must be finite and non-negative")
        if self.quantity > self.cumulative_quantity:
            raise ValueError("quantity exceeds cumulative_quantity")
        if self.cumulative_quantity > self.order_quantity:
            raise ValueError("cumulative_quantity exceeds order_quantity")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")

    def to_fill(self) -> Fill:
        self.validate()
        return Fill(
            fill_id=self.execution_fact_id,
            order_intent_id=self.intent_id,
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            price=self.price,
            fee=self.fee,
            occurred_at=self.occurred_at,
        )

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
            "quantity": str(self.quantity),
            "price": str(self.price),
            "fee": str(self.fee),
        }


@dataclass(frozen=True)
class StoredExecutionFact:
    fact: ExecutionFact
    state: ExecutionProjectionState
    reason: str | None


class ExecutionFactStore(Protocol):
    def append(self, fact: ExecutionFact, *, source_execution_id: str) -> bool: ...

    def mark_projected(self, execution_fact_id: str, *, occurred_at: datetime) -> None: ...

    def mark_quarantined(
        self,
        execution_fact_id: str,
        *,
        reason: str,
        occurred_at: datetime,
    ) -> None: ...

    def unresolved_count(self) -> int: ...

    def unresolved(self) -> tuple[StoredExecutionFact, ...]: ...


class SQLiteExecutionFactStore:
    """Durable immutable execution inbox with append-only projection events."""

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

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_facts (
                    execution_fact_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_fact_sources (
                    source_execution_id TEXT PRIMARY KEY,
                    execution_fact_id TEXT NOT NULL REFERENCES execution_facts(execution_fact_id),
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_projection_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    execution_fact_id TEXT NOT NULL REFERENCES execution_facts(execution_fact_id),
                    state TEXT NOT NULL CHECK(state IN ('QUARANTINED', 'PROJECTED')),
                    reason TEXT,
                    occurred_at TEXT NOT NULL
                );
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
    def _canonical_payload(fact: ExecutionFact) -> str:
        return json.dumps(fact.payload(), sort_keys=True, separators=(",", ":"))

    def append(self, fact: ExecutionFact, *, source_execution_id: str) -> bool:
        fact.validate()
        source_id = source_execution_id.strip()
        if not source_id:
            raise ValueError("source_execution_id is required")
        moment = self._aware(fact.occurred_at)
        payload = self._canonical_payload(fact)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO execution_facts
                (execution_fact_id, payload, occurred_at) VALUES (?, ?, ?)""",
                (fact.execution_fact_id, payload, moment.isoformat()),
            )
            appended = cursor.rowcount == 1
            if not appended:
                row = connection.execute(
                    """SELECT payload, occurred_at FROM execution_facts
                    WHERE execution_fact_id=?""",
                    (fact.execution_fact_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("execution fact lookup lost conflict")
                if str(row["payload"]) != payload or str(row["occurred_at"]) != moment.isoformat():
                    raise ValueError("EXECUTION_FACT_CONFLICT")
            source_row = connection.execute(
                """SELECT execution_fact_id FROM execution_fact_sources
                WHERE source_execution_id=?""",
                (source_id,),
            ).fetchone()
            if source_row is None:
                connection.execute(
                    """INSERT INTO execution_fact_sources
                    (source_execution_id, execution_fact_id, observed_at) VALUES (?, ?, ?)""",
                    (source_id, fact.execution_fact_id, moment.isoformat()),
                )
            elif str(source_row["execution_fact_id"]) != fact.execution_fact_id:
                raise ValueError("EXECUTION_SOURCE_ID_CONFLICT")
            connection.execute("COMMIT")
            return appended
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()

    def _append_projection_event(
        self,
        execution_fact_id: str,
        *,
        state: ExecutionProjectionState,
        reason: str | None,
        occurred_at: datetime,
    ) -> None:
        if state is ExecutionProjectionState.PENDING:
            raise ValueError("PENDING is represented by absence of projection events")
        fact_id = execution_fact_id.strip()
        if not fact_id:
            raise ValueError("execution_fact_id is required")
        if state is ExecutionProjectionState.QUARANTINED and not (reason or "").strip():
            raise ValueError("quarantine reason is required")
        moment = self._aware(occurred_at)
        normalized_reason = None if reason is None else reason.strip()
        event_id = f"execution-projection:{fact_id}:{state.value}:{normalized_reason or ''}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM execution_facts WHERE execution_fact_id=?",
                (fact_id,),
            ).fetchone() is None:
                raise KeyError(fact_id)
            current = connection.execute(
                """SELECT state FROM execution_projection_events
                WHERE execution_fact_id=? ORDER BY sequence DESC LIMIT 1""",
                (fact_id,),
            ).fetchone()
            if (
                current is not None
                and str(current["state"]) == ExecutionProjectionState.PROJECTED.value
                and state is not ExecutionProjectionState.PROJECTED
            ):
                raise ValueError("EXECUTION_PROJECTION_STATE_REGRESSION")
            connection.execute(
                """INSERT OR IGNORE INTO execution_projection_events
                (event_id, execution_fact_id, state, reason, occurred_at)
                VALUES (?, ?, ?, ?, ?)""",
                (event_id, fact_id, state.value, normalized_reason, moment.isoformat()),
            )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()

    def mark_projected(self, execution_fact_id: str, *, occurred_at: datetime) -> None:
        self._append_projection_event(
            execution_fact_id,
            state=ExecutionProjectionState.PROJECTED,
            reason=None,
            occurred_at=occurred_at,
        )

    def mark_quarantined(
        self,
        execution_fact_id: str,
        *,
        reason: str,
        occurred_at: datetime,
    ) -> None:
        self._append_projection_event(
            execution_fact_id,
            state=ExecutionProjectionState.QUARANTINED,
            reason=reason,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _fact(row: sqlite3.Row) -> ExecutionFact:
        payload = dict(json.loads(str(row["payload"])))
        return ExecutionFact(
            execution_fact_id=str(row["execution_fact_id"]),
            intent_id=str(payload["intent_id"]),
            broker_order_id=str(payload["broker_order_id"]),
            client_order_id=str(payload["client_order_id"]),
            symbol=str(payload["symbol"]),
            side=Side(str(payload["side"])),
            order_quantity=Decimal(str(payload["order_quantity"])),
            cumulative_quantity=Decimal(str(payload["cumulative_quantity"])),
            quantity=Decimal(str(payload["quantity"])),
            price=Decimal(str(payload["price"])),
            fee=Decimal(str(payload["fee"])),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
        )

    def unresolved(self) -> tuple[StoredExecutionFact, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT f.execution_fact_id, f.payload, f.occurred_at,
                    COALESCE((SELECT e.state FROM execution_projection_events e
                        WHERE e.execution_fact_id=f.execution_fact_id
                        ORDER BY e.sequence DESC LIMIT 1), 'PENDING') AS projection_state,
                    (SELECT e.reason FROM execution_projection_events e
                        WHERE e.execution_fact_id=f.execution_fact_id
                        ORDER BY e.sequence DESC LIMIT 1) AS projection_reason
                FROM execution_facts f
                WHERE COALESCE((SELECT e.state FROM execution_projection_events e
                        WHERE e.execution_fact_id=f.execution_fact_id
                        ORDER BY e.sequence DESC LIMIT 1), 'PENDING') != 'PROJECTED'
                ORDER BY f.occurred_at, f.execution_fact_id"""
            ).fetchall()
        finally:
            connection.close()
        return tuple(
            StoredExecutionFact(
                fact=self._fact(row),
                state=ExecutionProjectionState(str(row["projection_state"])),
                reason=None if row["projection_reason"] is None else str(row["projection_reason"]),
            )
            for row in rows
        )

    def unresolved_count(self) -> int:
        return len(self.unresolved())


class PostgresExecutionFactStore:
    """PostgreSQL execution inbox with conflict-aware facts and append-only projection events."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use PostgresExecutionFactStore")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(self, path: str | Path = "migrations/product/005_execution_facts.sql") -> None:
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
    def _canonical_payload(fact: ExecutionFact) -> dict[str, object]:
        return fact.payload()

    def append(self, fact: ExecutionFact, *, source_execution_id: str) -> bool:
        fact.validate()
        source_id = source_execution_id.strip()
        if not source_id:
            raise ValueError("source_execution_id is required")
        moment = self._aware(fact.occurred_at)
        payload = self._canonical_payload(fact)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_execution_facts
                        (execution_fact_id, payload, occurred_at)
                        VALUES (%s, %s::jsonb, %s)
                        ON CONFLICT (execution_fact_id) DO NOTHING""",
                        (fact.execution_fact_id, json.dumps(payload, sort_keys=True), moment),
                    )
                    appended = cursor.rowcount == 1
                    if not appended:
                        cursor.execute(
                            """SELECT payload, occurred_at FROM astra_execution_facts
                            WHERE execution_fact_id=%s FOR SHARE""",
                            (fact.execution_fact_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("execution fact lookup lost conflict")
                        existing_payload = row["payload"]
                        if isinstance(existing_payload, str):
                            existing_payload = json.loads(existing_payload)
                        existing_time = row["occurred_at"]
                        if not isinstance(existing_time, datetime):
                            existing_time = datetime.fromisoformat(str(existing_time))
                        if existing_payload != payload or self._aware(existing_time) != moment:
                            raise ValueError("EXECUTION_FACT_CONFLICT")
                    cursor.execute(
                        """INSERT INTO astra_execution_fact_sources
                        (source_execution_id, execution_fact_id, observed_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (source_execution_id) DO NOTHING""",
                        (source_id, fact.execution_fact_id, moment),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT execution_fact_id FROM astra_execution_fact_sources
                            WHERE source_execution_id=%s FOR SHARE""",
                            (source_id,),
                        )
                        source_row = cursor.fetchone()
                        if source_row is None:
                            raise RuntimeError("execution source lookup lost conflict")
                        if str(source_row["execution_fact_id"]) != fact.execution_fact_id:
                            raise ValueError("EXECUTION_SOURCE_ID_CONFLICT")
                    return appended

    def _append_projection_event(
        self,
        execution_fact_id: str,
        *,
        state: ExecutionProjectionState,
        reason: str | None,
        occurred_at: datetime,
    ) -> None:
        if state is ExecutionProjectionState.PENDING:
            raise ValueError("PENDING is represented by absence of projection events")
        fact_id = execution_fact_id.strip()
        if not fact_id:
            raise ValueError("execution_fact_id is required")
        if state is ExecutionProjectionState.QUARANTINED and not (reason or "").strip():
            raise ValueError("quarantine reason is required")
        moment = self._aware(occurred_at)
        normalized_reason = None if reason is None else reason.strip()
        event_id = f"execution-projection:{fact_id}:{state.value}:{normalized_reason or ''}"
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT 1 FROM astra_execution_facts
                        WHERE execution_fact_id=%s FOR UPDATE""",
                        (fact_id,),
                    )
                    if cursor.fetchone() is None:
                        raise KeyError(fact_id)
                    cursor.execute(
                        """SELECT state FROM astra_execution_projection_events
                        WHERE execution_fact_id=%s ORDER BY sequence DESC LIMIT 1""",
                        (fact_id,),
                    )
                    current = cursor.fetchone()
                    if (
                        current is not None
                        and str(current["state"]) == ExecutionProjectionState.PROJECTED.value
                        and state is not ExecutionProjectionState.PROJECTED
                    ):
                        raise ValueError("EXECUTION_PROJECTION_STATE_REGRESSION")
                    cursor.execute(
                        """INSERT INTO astra_execution_projection_events
                        (event_id, execution_fact_id, state, reason, occurred_at)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (event_id) DO NOTHING""",
                        (event_id, fact_id, state.value, normalized_reason, moment),
                    )

    def mark_projected(self, execution_fact_id: str, *, occurred_at: datetime) -> None:
        self._append_projection_event(
            execution_fact_id,
            state=ExecutionProjectionState.PROJECTED,
            reason=None,
            occurred_at=occurred_at,
        )

    def mark_quarantined(
        self,
        execution_fact_id: str,
        *,
        reason: str,
        occurred_at: datetime,
    ) -> None:
        self._append_projection_event(
            execution_fact_id,
            state=ExecutionProjectionState.QUARANTINED,
            reason=reason,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _fact(row: dict[str, object]) -> ExecutionFact:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise RuntimeError("invalid execution fact payload")
        occurred_at = row["occurred_at"]
        if not isinstance(occurred_at, datetime):
            occurred_at = datetime.fromisoformat(str(occurred_at))
        return ExecutionFact(
            execution_fact_id=str(row["execution_fact_id"]),
            intent_id=str(payload["intent_id"]),
            broker_order_id=str(payload["broker_order_id"]),
            client_order_id=str(payload["client_order_id"]),
            symbol=str(payload["symbol"]),
            side=Side(str(payload["side"])),
            order_quantity=Decimal(str(payload["order_quantity"])),
            cumulative_quantity=Decimal(str(payload["cumulative_quantity"])),
            quantity=Decimal(str(payload["quantity"])),
            price=Decimal(str(payload["price"])),
            fee=Decimal(str(payload["fee"])),
            occurred_at=occurred_at,
        )

    def unresolved(self) -> tuple[StoredExecutionFact, ...]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT f.execution_fact_id, f.payload, f.occurred_at,
                        COALESCE((SELECT e.state FROM astra_execution_projection_events e
                            WHERE e.execution_fact_id=f.execution_fact_id
                            ORDER BY e.sequence DESC LIMIT 1), 'PENDING') AS projection_state,
                        (SELECT e.reason FROM astra_execution_projection_events e
                            WHERE e.execution_fact_id=f.execution_fact_id
                            ORDER BY e.sequence DESC LIMIT 1) AS projection_reason
                    FROM astra_execution_facts f
                    WHERE COALESCE((SELECT e.state FROM astra_execution_projection_events e
                            WHERE e.execution_fact_id=f.execution_fact_id
                            ORDER BY e.sequence DESC LIMIT 1), 'PENDING') != 'PROJECTED'
                    ORDER BY f.occurred_at, f.execution_fact_id"""
                )
                rows = cursor.fetchall()
        return tuple(
            StoredExecutionFact(
                fact=self._fact(dict(row)),
                state=ExecutionProjectionState(str(row["projection_state"])),
                reason=None if row["projection_reason"] is None else str(row["projection_reason"]),
            )
            for row in rows
        )

    def unresolved_count(self) -> int:
        return len(self.unresolved())
