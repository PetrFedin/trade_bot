from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from app.domain.trading import OrderIntent


class OrderLimitEventKind(StrEnum):
    INITIAL = "INITIAL"
    REPLACE_CONFIRMED = "REPLACE_CONFIRMED"


@dataclass(frozen=True)
class ConfirmedOrderLimitEvent:
    event_id: str
    intent_id: str
    event_kind: OrderLimitEventKind
    limit_price: Decimal
    effective_at: datetime
    mutation_id: str | None = None
    broker_order_id: str | None = None

    def validate(self) -> None:
        if not self.event_id.strip() or not self.intent_id.strip():
            raise ValueError("order limit event identity is required")
        if not self.limit_price.is_finite() or self.limit_price <= 0:
            raise ValueError("limit_price must be positive and finite")
        if self.effective_at.tzinfo is None or self.effective_at.utcoffset() is None:
            raise ValueError("effective_at must be timezone-aware")
        if self.event_kind is OrderLimitEventKind.INITIAL and self.mutation_id is not None:
            raise ValueError("initial order limit cannot have mutation_id")
        if self.event_kind is OrderLimitEventKind.REPLACE_CONFIRMED:
            if self.mutation_id is None or not self.mutation_id.strip():
                raise ValueError("confirmed replacement requires mutation_id")


class SQLiteConfirmedOrderLimitHistory:
    """Durable timeline of limits known to have been effective at the broker.

    Requested, started, failed or uncertain replacements are intentionally excluded.
    A replacement is recorded only after broker confirmation/reconciliation has proven
    the new limit became effective.
    """

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

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS confirmed_order_limit_history (
                    event_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    limit_price TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    mutation_id TEXT,
                    broker_order_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_confirmed_order_limit_history_intent_time
                ON confirmed_order_limit_history(intent_id, effective_at, event_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_confirmed_order_limit_history_mutation
                ON confirmed_order_limit_history(mutation_id)
                WHERE mutation_id IS NOT NULL;
                """
            )
        finally:
            connection.close()

    def record_initial(
        self,
        intent: OrderIntent,
        *,
        effective_at: datetime | None = None,
    ) -> ConfirmedOrderLimitEvent:
        intent.validate()
        moment = intent.created_at if effective_at is None else effective_at
        event = ConfirmedOrderLimitEvent(
            event_id=f"initial:{intent.intent_id}",
            intent_id=intent.intent_id,
            event_kind=OrderLimitEventKind.INITIAL,
            limit_price=intent.limit_price,
            effective_at=_aware(moment, field_name="effective_at"),
        )
        return self.append(event)

    def record_confirmed_replace(
        self,
        *,
        intent_id: str,
        mutation_id: str,
        limit_price: Decimal,
        confirmed_at: datetime,
        broker_order_id: str | None = None,
    ) -> ConfirmedOrderLimitEvent:
        if not mutation_id.strip():
            raise ValueError("mutation_id is required")
        event = ConfirmedOrderLimitEvent(
            event_id=f"replace:{mutation_id}",
            intent_id=intent_id,
            event_kind=OrderLimitEventKind.REPLACE_CONFIRMED,
            limit_price=limit_price,
            effective_at=_aware(confirmed_at, field_name="confirmed_at"),
            mutation_id=mutation_id,
            broker_order_id=(
                None if broker_order_id is None else broker_order_id.strip() or None
            ),
        )
        return self.append(event)

    def append(self, event: ConfirmedOrderLimitEvent) -> ConfirmedOrderLimitEvent:
        event.validate()
        normalized = ConfirmedOrderLimitEvent(
            event_id=event.event_id,
            intent_id=event.intent_id,
            event_kind=event.event_kind,
            limit_price=event.limit_price,
            effective_at=event.effective_at.astimezone(UTC),
            mutation_id=event.mutation_id,
            broker_order_id=event.broker_order_id,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM confirmed_order_limit_history WHERE event_id=?",
                (normalized.event_id,),
            ).fetchone()
            if existing is not None:
                current = self._row(existing)
                if current != normalized:
                    raise ValueError("CONFIRMED_ORDER_LIMIT_EVENT_CONFLICT")
                connection.execute("COMMIT")
                return current
            if normalized.mutation_id is not None:
                by_mutation = connection.execute(
                    """SELECT * FROM confirmed_order_limit_history
                    WHERE mutation_id=?""",
                    (normalized.mutation_id,),
                ).fetchone()
                if by_mutation is not None:
                    current = self._row(by_mutation)
                    if current != normalized:
                        raise ValueError("CONFIRMED_ORDER_LIMIT_MUTATION_CONFLICT")
                    connection.execute("COMMIT")
                    return current
            connection.execute(
                """INSERT INTO confirmed_order_limit_history (
                    event_id, intent_id, event_kind, limit_price, effective_at,
                    mutation_id, broker_order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized.event_id,
                    normalized.intent_id,
                    normalized.event_kind.value,
                    str(normalized.limit_price),
                    normalized.effective_at.isoformat(),
                    normalized.mutation_id,
                    normalized.broker_order_id,
                ),
            )
            connection.execute("COMMIT")
            return normalized
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def events(self, intent_id: str) -> tuple[ConfirmedOrderLimitEvent, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT * FROM confirmed_order_limit_history
                WHERE intent_id=? ORDER BY effective_at, event_id""",
                (intent_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._row(row) for row in rows)

    def limit_price_for_fill(
        self,
        intent_id: str,
        *,
        occurred_at: datetime,
        fallback: Decimal,
    ) -> Decimal:
        if not fallback.is_finite() or fallback <= 0:
            raise ValueError("fallback limit must be positive and finite")
        moment = _aware(occurred_at, field_name="occurred_at")
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT * FROM confirmed_order_limit_history
                WHERE intent_id=? AND effective_at<=?
                ORDER BY effective_at DESC, event_id DESC LIMIT 1""",
                (intent_id, moment.isoformat()),
            ).fetchone()
        finally:
            connection.close()
        return fallback if row is None else self._row(row).limit_price

    @staticmethod
    def _row(row: sqlite3.Row) -> ConfirmedOrderLimitEvent:
        event = ConfirmedOrderLimitEvent(
            event_id=str(row["event_id"]),
            intent_id=str(row["intent_id"]),
            event_kind=OrderLimitEventKind(str(row["event_kind"])),
            limit_price=Decimal(str(row["limit_price"])),
            effective_at=datetime.fromisoformat(str(row["effective_at"])),
            mutation_id=None if row["mutation_id"] is None else str(row["mutation_id"]),
            broker_order_id=(
                None if row["broker_order_id"] is None else str(row["broker_order_id"])
            ),
        )
        event.validate()
        return event


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)
