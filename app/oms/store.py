from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from app.domain.trading import OrderIntent, Side

UTC = UTC


class OrderState(StrEnum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    OUTBOXED = "OUTBOXED"
    SUBMIT_STARTED = "SUBMIT_STARTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    RECONCILING = "RECONCILING"
    RECONCILED = "RECONCILED"
    MANUAL = "MANUAL"


_TERMINAL = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED})


_ALLOWED: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.RISK_APPROVED, OrderState.REJECTED}),
    OrderState.RISK_APPROVED: frozenset({OrderState.OUTBOXED}),
    OrderState.OUTBOXED: frozenset({OrderState.SUBMIT_STARTED}),
    OrderState.SUBMIT_STARTED: frozenset(
        {OrderState.ACKNOWLEDGED, OrderState.REJECTED, OrderState.UNCERTAIN}
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELLED,
            OrderState.UNCERTAIN,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELLED,
            OrderState.UNCERTAIN,
        }
    ),
    OrderState.CANCEL_REQUESTED: frozenset(
        {OrderState.CANCELLED, OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.UNCERTAIN}
    ),
    OrderState.UNCERTAIN: frozenset({OrderState.RECONCILING, OrderState.MANUAL}),
    OrderState.RECONCILING: frozenset({OrderState.RECONCILED, OrderState.MANUAL}),
    OrderState.RECONCILED: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.MANUAL,
        }
    ),
    OrderState.MANUAL: frozenset(),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
}


@dataclass(frozen=True)
class OrderRecord:
    intent_id: str
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    limit_price: Decimal
    filled_quantity: Decimal
    state: OrderState
    version: int
    updated_at: datetime

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL


@dataclass(frozen=True)
class OutboxMessage:
    message_id: int
    intent_id: str
    topic: str
    payload: dict[str, object]
    created_at: datetime


class DurableOmsStore:
    """Transactional SQLite OMS with append-only events and a durable submit outbox."""

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
                CREATE TABLE IF NOT EXISTS oms_orders (
                    intent_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    broker_order_id TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    limit_price TEXT NOT NULL,
                    filled_quantity TEXT NOT NULL DEFAULT '0',
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oms_events (
                    event_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES oms_orders(intent_id),
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oms_outbox (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL REFERENCES oms_orders(intent_id),
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_oms_outbox_submit_once
                    ON oms_outbox(intent_id, topic);
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _now(value: datetime | None = None) -> datetime:
        moment = datetime.now(UTC) if value is None else value
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return moment.astimezone(UTC)

    @staticmethod
    def _row(row: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            intent_id=str(row["intent_id"]),
            client_order_id=str(row["client_order_id"]),
            broker_order_id=str(row["broker_order_id"]),
            symbol=str(row["symbol"]),
            side=Side(str(row["side"])),
            quantity=Decimal(str(row["quantity"])),
            limit_price=Decimal(str(row["limit_price"])),
            filled_quantity=Decimal(str(row["filled_quantity"])),
            state=OrderState(str(row["state"])),
            version=int(row["version"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def get(self, intent_id: str) -> OrderRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM oms_orders WHERE intent_id=?", (intent_id,)
            ).fetchone()
            return None if row is None else self._row(row)
        finally:
            connection.close()

    def create(
        self, intent: OrderIntent, *, client_order_id: str, occurred_at: datetime | None = None
    ) -> OrderRecord:
        intent.validate()
        if not client_order_id.strip():
            raise ValueError("client_order_id is required")
        moment = self._now(occurred_at or intent.created_at)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM oms_orders WHERE intent_id=?", (intent.intent_id,)
            ).fetchone()
            if existing is not None:
                record = self._row(existing)
                if record.client_order_id != client_order_id:
                    raise ValueError("intent already exists with different client_order_id")
                return record
            connection.execute(
                """INSERT INTO oms_orders
                (intent_id, client_order_id, symbol, side, quantity, limit_price,
                 filled_quantity, state, version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, '0', ?, 1, ?)""",
                (
                    intent.intent_id,
                    client_order_id,
                    intent.symbol,
                    intent.side.value,
                    str(intent.quantity),
                    str(intent.limit_price),
                    OrderState.CREATED.value,
                    moment.isoformat(),
                ),
            )
            self._append_event(
                connection,
                event_id=f"create:{intent.intent_id}",
                intent_id=intent.intent_id,
                event_type="CREATED",
                payload={"client_order_id": client_order_id},
                occurred_at=moment,
            )
        created = self.get(intent.intent_id)
        assert created is not None
        return created

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        intent_id: str,
        event_type: str,
        payload: dict[str, object],
        occurred_at: datetime,
    ) -> bool:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO oms_events
            (event_id, intent_id, event_type, payload, occurred_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                event_id,
                intent_id,
                event_type,
                json.dumps(payload, sort_keys=True),
                occurred_at.isoformat(),
            ),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _load_for_update(connection: sqlite3.Connection, intent_id: str) -> OrderRecord:
        row = connection.execute(
            "SELECT * FROM oms_orders WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return DurableOmsStore._row(row)

    @staticmethod
    def _validate_transition(current: OrderState, target: OrderState) -> None:
        if target == current and current is OrderState.PARTIALLY_FILLED:
            return
        if target not in _ALLOWED[current]:
            raise ValueError(f"invalid OMS transition: {current.value}->{target.value}")

    def transition(
        self,
        intent_id: str,
        target: OrderState,
        *,
        event_id: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> OrderRecord:
        moment = self._now(occurred_at)
        with self._transaction() as connection:
            current = self._load_for_update(connection, intent_id)
            if connection.execute(
                "SELECT 1 FROM oms_events WHERE event_id=?", (event_id,)
            ).fetchone():
                return current
            self._validate_transition(current.state, target)
            broker_id = (
                current.broker_order_id if broker_order_id is None else broker_order_id.strip()
            )
            connection.execute(
                """UPDATE oms_orders SET state=?, broker_order_id=?, version=version+1, updated_at=?
                WHERE intent_id=?""",
                (target.value, broker_id, moment.isoformat(), intent_id),
            )
            self._append_event(
                connection,
                event_id=event_id,
                intent_id=intent_id,
                event_type=target.value,
                payload={} if payload is None else payload,
                occurred_at=moment,
            )
        result = self.get(intent_id)
        assert result is not None
        return result

    def approve_risk(self, intent_id: str, *, event_id: str, occurred_at: datetime) -> OrderRecord:
        return self.transition(
            intent_id, OrderState.RISK_APPROVED, event_id=event_id, occurred_at=occurred_at
        )

    def enqueue_submit(
        self, intent_id: str, *, event_id: str, occurred_at: datetime
    ) -> OrderRecord:
        moment = self._now(occurred_at)
        with self._transaction() as connection:
            current = self._load_for_update(connection, intent_id)
            if connection.execute(
                "SELECT 1 FROM oms_events WHERE event_id=?", (event_id,)
            ).fetchone():
                return current
            self._validate_transition(current.state, OrderState.OUTBOXED)
            payload = {
                "intent_id": current.intent_id,
                "client_order_id": current.client_order_id,
                "symbol": current.symbol,
                "side": current.side.value,
                "quantity": str(current.quantity),
                "limit_price": str(current.limit_price),
            }
            connection.execute(
                "UPDATE oms_orders SET state=?, version=version+1, updated_at=? WHERE intent_id=?",
                (OrderState.OUTBOXED.value, moment.isoformat(), intent_id),
            )
            connection.execute(
                """INSERT INTO oms_outbox(intent_id, topic, payload, created_at)
                VALUES (?, 'paper_order_submit', ?, ?)""",
                (intent_id, json.dumps(payload, sort_keys=True), moment.isoformat()),
            )
            self._append_event(
                connection,
                event_id=event_id,
                intent_id=intent_id,
                event_type=OrderState.OUTBOXED.value,
                payload=payload,
                occurred_at=moment,
            )
        result = self.get(intent_id)
        assert result is not None
        return result

    def pending_outbox(self, *, limit: int = 100) -> tuple[OutboxMessage, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT message_id, intent_id, topic, payload, created_at
                FROM oms_outbox WHERE published_at IS NULL ORDER BY message_id LIMIT ?""",
                (limit,),
            ).fetchall()
            return tuple(
                OutboxMessage(
                    message_id=int(row["message_id"]),
                    intent_id=str(row["intent_id"]),
                    topic=str(row["topic"]),
                    payload=dict(json.loads(str(row["payload"]))),
                    created_at=datetime.fromisoformat(str(row["created_at"])),
                )
                for row in rows
            )
        finally:
            connection.close()

    def mark_outbox_published(self, message_id: int, *, occurred_at: datetime) -> None:
        moment = self._now(occurred_at)
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE oms_outbox SET published_at=COALESCE(published_at, ?) WHERE message_id=?",
                (moment.isoformat(), message_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(message_id)

    def apply_cumulative_fill(
        self,
        intent_id: str,
        *,
        event_id: str,
        cumulative_filled: Decimal,
        occurred_at: datetime,
        broker_order_id: str | None = None,
    ) -> OrderRecord:
        if not cumulative_filled.is_finite() or cumulative_filled < 0:
            raise ValueError("cumulative_filled must be finite and non-negative")
        moment = self._now(occurred_at)
        with self._transaction() as connection:
            current = self._load_for_update(connection, intent_id)
            if connection.execute(
                "SELECT 1 FROM oms_events WHERE event_id=?", (event_id,)
            ).fetchone():
                return current
            if cumulative_filled < current.filled_quantity:
                raise ValueError("FILLED_QUANTITY_REGRESSION")
            if cumulative_filled > current.quantity:
                raise ValueError("FILLED_QUANTITY_EXCEEDS_ORDER")
            if cumulative_filled == 0:
                target = current.state
            elif cumulative_filled == current.quantity:
                target = OrderState.FILLED
            else:
                target = OrderState.PARTIALLY_FILLED
            if target != current.state:
                self._validate_transition(current.state, target)
            broker_id = (
                current.broker_order_id if broker_order_id is None else broker_order_id.strip()
            )
            connection.execute(
                """UPDATE oms_orders SET state=?, broker_order_id=?, filled_quantity=?,
                version=version+1, updated_at=? WHERE intent_id=?""",
                (target.value, broker_id, str(cumulative_filled), moment.isoformat(), intent_id),
            )
            self._append_event(
                connection,
                event_id=event_id,
                intent_id=intent_id,
                event_type="FILL_UPDATE",
                payload={"cumulative_filled": str(cumulative_filled), "state": target.value},
                occurred_at=moment,
            )
        result = self.get(intent_id)
        assert result is not None
        return result

    def events(self, intent_id: str) -> tuple[dict[str, object], ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT event_id, event_type, payload, occurred_at "
                "FROM oms_events WHERE intent_id=? ORDER BY rowid",
                (intent_id,),
            ).fetchall()
            return tuple(
                {
                    "event_id": str(row["event_id"]),
                    "event_type": str(row["event_type"]),
                    "payload": dict(json.loads(str(row["payload"]))),
                    "occurred_at": str(row["occurred_at"]),
                }
                for row in rows
            )
        finally:
            connection.close()
