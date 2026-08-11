from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.domain.trading import Fill, OrderIntent, Side
from app.execution.trade_fills import PaperFillObserver


@dataclass(frozen=True)
class PaperStrategyIntent:
    intent_id: str
    strategy_id: str
    symbol: str
    side: Side
    registered_at: datetime

    def validate(self) -> None:
        if not self.intent_id.strip() or not self.strategy_id.strip():
            raise ValueError("strategy intent identity is required")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be normalized uppercase")
        if self.registered_at.tzinfo is None or self.registered_at.utcoffset() is None:
            raise ValueError("registered_at must be timezone-aware")


class SQLitePaperStrategyIntentRegistry:
    """Durable intent ownership used to route exact fills to one strategy only."""

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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS paper_strategy_intents (
                    intent_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                )"""
            )
        finally:
            connection.close()

    def register(
        self,
        intent: OrderIntent,
        *,
        strategy_id: str | None = None,
        registered_at: datetime | None = None,
    ) -> PaperStrategyIntent:
        intent.validate()
        resolved_strategy = intent.strategy_id if strategy_id is None else strategy_id
        if not resolved_strategy.strip():
            raise ValueError("strategy_id is required")
        moment = intent.created_at if registered_at is None else registered_at
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("registered_at must be timezone-aware")
        record = PaperStrategyIntent(
            intent_id=intent.intent_id,
            strategy_id=resolved_strategy.strip(),
            symbol=intent.symbol,
            side=intent.side,
            registered_at=moment.astimezone(UTC),
        )
        record.validate()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM paper_strategy_intents WHERE intent_id=?",
                (record.intent_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO paper_strategy_intents
                    (intent_id, strategy_id, symbol, side, registered_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        record.intent_id,
                        record.strategy_id,
                        record.symbol,
                        record.side.value,
                        record.registered_at.isoformat(),
                    ),
                )
            else:
                current = self._row(existing)
                if current != record:
                    raise ValueError("PAPER_STRATEGY_INTENT_CONFLICT")
            connection.execute("COMMIT")
            return record
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get(self, intent_id: str) -> PaperStrategyIntent | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM paper_strategy_intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._row(row)

    @staticmethod
    def _row(row: sqlite3.Row) -> PaperStrategyIntent:
        record = PaperStrategyIntent(
            intent_id=str(row["intent_id"]),
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
            side=Side(str(row["side"])),
            registered_at=datetime.fromisoformat(str(row["registered_at"])),
        )
        record.validate()
        return record


class StrategyScopedPaperFillObserver:
    """Route exact fills only when durable intent ownership matches this strategy."""

    def __init__(
        self,
        *,
        strategy_id: str,
        registry: SQLitePaperStrategyIntentRegistry,
        observer: PaperFillObserver,
    ) -> None:
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        self.strategy_id = strategy_id.strip()
        self.registry = registry
        self.observer = observer

    def observe_fill(self, fill: Fill) -> None:
        fill.validate()
        ownership = self.registry.get(fill.order_intent_id)
        if ownership is None or ownership.strategy_id != self.strategy_id:
            return
        if ownership.symbol != fill.symbol:
            raise ValueError("PAPER_STRATEGY_FILL_SYMBOL_MISMATCH")
        if ownership.side is not fill.side:
            raise ValueError("PAPER_STRATEGY_FILL_SIDE_MISMATCH")
        self.observer.observe_fill(fill)
