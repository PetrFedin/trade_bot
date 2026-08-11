from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.application.paper_strategy_scope import SQLitePaperStrategyIntentRegistry
from app.domain.trading import Fill, Side


@dataclass(frozen=True)
class PaperReactionFill:
    fill_id: str
    intent_id: str
    strategy_id: str
    symbol: str
    side: Side
    decision_at: datetime
    fill_at: datetime
    latency_seconds: Decimal


@dataclass(frozen=True)
class PaperReactionSummary:
    fill_count: int
    average_latency_seconds: Decimal | None
    maximum_latency_seconds: Decimal | None
    p95_latency_seconds: Decimal | None


class SQLitePaperReactionQualityStore:
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
                """CREATE TABLE IF NOT EXISTS paper_reaction_quality (
                    fill_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    decision_at TEXT NOT NULL,
                    fill_at TEXT NOT NULL,
                    latency_seconds TEXT NOT NULL
                )"""
            )
        finally:
            connection.close()

    def append(self, observation: PaperReactionFill) -> bool:
        self._validate(observation)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM paper_reaction_quality WHERE fill_id=?",
                (observation.fill_id,),
            ).fetchone()
            if row is not None:
                if self._row(row) != observation:
                    raise ValueError("PAPER_REACTION_FILL_CONFLICT")
                connection.execute("COMMIT")
                return False
            connection.execute(
                """INSERT INTO paper_reaction_quality (
                    fill_id, intent_id, strategy_id, symbol, side,
                    decision_at, fill_at, latency_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.fill_id,
                    observation.intent_id,
                    observation.strategy_id,
                    observation.symbol,
                    observation.side.value,
                    observation.decision_at.isoformat(),
                    observation.fill_at.isoformat(),
                    str(observation.latency_seconds),
                ),
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def fills(
        self,
        *,
        strategy_id: str,
        side: Side | None = None,
    ) -> tuple[PaperReactionFill, ...]:
        connection = self._connect()
        try:
            if side is None:
                rows = connection.execute(
                    """SELECT * FROM paper_reaction_quality
                    WHERE strategy_id=? ORDER BY fill_at, fill_id""",
                    (strategy_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM paper_reaction_quality
                    WHERE strategy_id=? AND side=? ORDER BY fill_at, fill_id""",
                    (strategy_id, side.value),
                ).fetchall()
        finally:
            connection.close()
        return tuple(self._row(row) for row in rows)

    def summary(
        self,
        *,
        strategy_id: str,
        side: Side | None = None,
    ) -> PaperReactionSummary:
        observations = self.fills(strategy_id=strategy_id, side=side)
        latencies = tuple(sorted(item.latency_seconds for item in observations))
        if not latencies:
            return PaperReactionSummary(
                fill_count=0,
                average_latency_seconds=None,
                maximum_latency_seconds=None,
                p95_latency_seconds=None,
            )
        average = sum(latencies, start=Decimal("0")) / Decimal(len(latencies))
        p95_index = max(0, (95 * len(latencies) + 99) // 100 - 1)
        return PaperReactionSummary(
            fill_count=len(latencies),
            average_latency_seconds=average,
            maximum_latency_seconds=latencies[-1],
            p95_latency_seconds=latencies[p95_index],
        )

    @staticmethod
    def _validate(observation: PaperReactionFill) -> None:
        if not observation.fill_id.strip() or not observation.intent_id.strip():
            raise ValueError("reaction fill identity is required")
        if not observation.strategy_id.strip():
            raise ValueError("reaction strategy_id is required")
        if observation.decision_at.tzinfo is None or observation.decision_at.utcoffset() is None:
            raise ValueError("reaction decision_at must be timezone-aware")
        if observation.fill_at.tzinfo is None or observation.fill_at.utcoffset() is None:
            raise ValueError("reaction fill_at must be timezone-aware")
        if not observation.latency_seconds.is_finite() or observation.latency_seconds < 0:
            raise ValueError("reaction latency must be finite and non-negative")

    @staticmethod
    def _row(row: sqlite3.Row) -> PaperReactionFill:
        observation = PaperReactionFill(
            fill_id=str(row["fill_id"]),
            intent_id=str(row["intent_id"]),
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
            side=Side(str(row["side"])),
            decision_at=datetime.fromisoformat(str(row["decision_at"])),
            fill_at=datetime.fromisoformat(str(row["fill_at"])),
            latency_seconds=Decimal(str(row["latency_seconds"])),
        )
        SQLitePaperReactionQualityStore._validate(observation)
        return observation


class StrategyPaperReactionTracker:
    """PaperFillObserver for decision-to-fill latency of one registered strategy."""

    def __init__(
        self,
        *,
        strategy_id: str,
        registry: SQLitePaperStrategyIntentRegistry,
        store: SQLitePaperReactionQualityStore,
    ) -> None:
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        self.strategy_id = strategy_id.strip()
        self.registry = registry
        self.store = store

    def observe_fill(self, fill: Fill) -> None:
        fill.validate()
        ownership = self.registry.get(fill.order_intent_id)
        if ownership is None or ownership.strategy_id != self.strategy_id:
            return
        if ownership.symbol != fill.symbol or ownership.side is not fill.side:
            raise ValueError("PAPER_REACTION_FILL_IDENTITY_MISMATCH")
        latency = Decimal(str((fill.occurred_at - ownership.registered_at).total_seconds()))
        if latency < 0:
            raise ValueError("PAPER_REACTION_FILL_PRECEDES_DECISION")
        self.store.append(
            PaperReactionFill(
                fill_id=fill.fill_id,
                intent_id=fill.order_intent_id,
                strategy_id=self.strategy_id,
                symbol=fill.symbol,
                side=fill.side,
                decision_at=ownership.registered_at,
                fill_at=fill.occurred_at,
                latency_seconds=latency,
            )
        )
