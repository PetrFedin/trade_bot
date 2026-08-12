from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from app.domain.trading import Fill, Side
from app.oms.protocols import OmsStore


class EffectiveLimitProvider(Protocol):
    def current_limit_price(self, intent_id: str, *, fallback: Decimal) -> Decimal: ...


@dataclass(frozen=True)
class PaperExecutionQualityFill:
    fill_id: str
    intent_id: str
    symbol: str
    side: Side
    quantity: Decimal
    expected_limit_price: Decimal
    fill_price: Decimal
    signed_slippage_fraction: Decimal
    signed_slippage_notional: Decimal
    occurred_at: datetime

    @property
    def signed_slippage_bps(self) -> Decimal:
        return self.signed_slippage_fraction * Decimal("10000")


@dataclass(frozen=True)
class PaperExecutionQualitySummary:
    fill_count: int
    adverse_fill_count: int
    favorable_fill_count: int
    flat_fill_count: int
    expected_notional: Decimal
    signed_slippage_notional: Decimal
    weighted_signed_slippage_bps: Decimal | None
    worst_signed_slippage_bps: Decimal | None


class SQLitePaperExecutionQualityStore:
    """Idempotent exact-fill execution evidence with positive values = worse fills."""

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
                """CREATE TABLE IF NOT EXISTS paper_execution_quality (
                    fill_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    expected_limit_price TEXT NOT NULL,
                    fill_price TEXT NOT NULL,
                    signed_slippage_fraction TEXT NOT NULL,
                    signed_slippage_notional TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                )"""
            )
        finally:
            connection.close()

    def append(self, observation: PaperExecutionQualityFill) -> bool:
        self._validate(observation)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM paper_execution_quality WHERE fill_id=?",
                (observation.fill_id,),
            ).fetchone()
            if existing is not None:
                if self._row(existing) != observation:
                    raise ValueError("PAPER_EXECUTION_QUALITY_FILL_CONFLICT")
                connection.execute("COMMIT")
                return False
            connection.execute(
                """INSERT INTO paper_execution_quality (
                    fill_id, intent_id, symbol, side, quantity,
                    expected_limit_price, fill_price, signed_slippage_fraction,
                    signed_slippage_notional, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.fill_id,
                    observation.intent_id,
                    observation.symbol,
                    observation.side.value,
                    str(observation.quantity),
                    str(observation.expected_limit_price),
                    str(observation.fill_price),
                    str(observation.signed_slippage_fraction),
                    str(observation.signed_slippage_notional),
                    observation.occurred_at.isoformat(),
                ),
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def fills(self, *, side: Side | None = None) -> tuple[PaperExecutionQualityFill, ...]:
        connection = self._connect()
        try:
            if side is None:
                rows = connection.execute(
                    "SELECT * FROM paper_execution_quality ORDER BY occurred_at, fill_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM paper_execution_quality
                    WHERE side=? ORDER BY occurred_at, fill_id""",
                    (side.value,),
                ).fetchall()
        finally:
            connection.close()
        return tuple(self._row(row) for row in rows)

    def summary(self, *, side: Side | None = None) -> PaperExecutionQualitySummary:
        observations = self.fills(side=side)
        expected_notional = sum(
            (
                observation.expected_limit_price * observation.quantity
                for observation in observations
            ),
            start=Decimal("0"),
        )
        signed_notional = sum(
            (observation.signed_slippage_notional for observation in observations),
            start=Decimal("0"),
        )
        weighted_bps = (
            None
            if expected_notional == 0
            else signed_notional / expected_notional * Decimal("10000")
        )
        worst = (
            None
            if not observations
            else max(observation.signed_slippage_bps for observation in observations)
        )
        return PaperExecutionQualitySummary(
            fill_count=len(observations),
            adverse_fill_count=sum(
                observation.signed_slippage_fraction > 0
                for observation in observations
            ),
            favorable_fill_count=sum(
                observation.signed_slippage_fraction < 0
                for observation in observations
            ),
            flat_fill_count=sum(
                observation.signed_slippage_fraction == 0
                for observation in observations
            ),
            expected_notional=expected_notional,
            signed_slippage_notional=signed_notional,
            weighted_signed_slippage_bps=weighted_bps,
            worst_signed_slippage_bps=worst,
        )

    @staticmethod
    def _validate(observation: PaperExecutionQualityFill) -> None:
        if not observation.fill_id.strip() or not observation.intent_id.strip():
            raise ValueError("execution quality identity is required")
        if not observation.symbol or observation.symbol != observation.symbol.upper():
            raise ValueError("execution quality symbol must be uppercase")
        if observation.occurred_at.tzinfo is None or observation.occurred_at.utcoffset() is None:
            raise ValueError("execution quality timestamp must be timezone-aware")
        for field_name, value in (
            ("quantity", observation.quantity),
            ("expected_limit_price", observation.expected_limit_price),
            ("fill_price", observation.fill_price),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{field_name} must be positive and finite")
        for field_name, value in (
            ("signed_slippage_fraction", observation.signed_slippage_fraction),
            ("signed_slippage_notional", observation.signed_slippage_notional),
        ):
            if not value.is_finite():
                raise ValueError(f"{field_name} must be finite")

    @staticmethod
    def _row(row: sqlite3.Row) -> PaperExecutionQualityFill:
        observation = PaperExecutionQualityFill(
            fill_id=str(row["fill_id"]),
            intent_id=str(row["intent_id"]),
            symbol=str(row["symbol"]),
            side=Side(str(row["side"])),
            quantity=Decimal(str(row["quantity"])),
            expected_limit_price=Decimal(str(row["expected_limit_price"])),
            fill_price=Decimal(str(row["fill_price"])),
            signed_slippage_fraction=Decimal(str(row["signed_slippage_fraction"])),
            signed_slippage_notional=Decimal(str(row["signed_slippage_notional"])),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
        )
        SQLitePaperExecutionQualityStore._validate(observation)
        return observation


class PaperExecutionQualityTracker:
    """PaperFillObserver that compares exact fills with the effective submitted limit."""

    def __init__(
        self,
        *,
        oms: OmsStore,
        store: SQLitePaperExecutionQualityStore,
        effective_limits: EffectiveLimitProvider | None = None,
    ) -> None:
        self.oms = oms
        self.store = store
        self.effective_limits = effective_limits

    def observe_fill(self, fill: Fill) -> None:
        fill.validate()
        order = self.oms.get(fill.order_intent_id)
        if order is None:
            raise KeyError(fill.order_intent_id)
        if order.symbol != fill.symbol or order.side is not fill.side:
            raise ValueError("PAPER_EXECUTION_QUALITY_ORDER_IDENTITY_MISMATCH")
        expected = order.limit_price
        if self.effective_limits is not None:
            expected = self.effective_limits.current_limit_price(
                fill.order_intent_id,
                fallback=expected,
            )
        price_delta = fill.price - expected
        raw_fraction = price_delta / expected
        signed_fraction = raw_fraction if fill.side is Side.BUY else -raw_fraction
        signed_notional = (
            price_delta * fill.quantity
            if fill.side is Side.BUY
            else -price_delta * fill.quantity
        )
        self.store.append(
            PaperExecutionQualityFill(
                fill_id=fill.fill_id,
                intent_id=fill.order_intent_id,
                symbol=fill.symbol,
                side=fill.side,
                quantity=fill.quantity,
                expected_limit_price=expected,
                fill_price=fill.price,
                signed_slippage_fraction=signed_fraction,
                signed_slippage_notional=signed_notional,
                occurred_at=fill.occurred_at,
            )
        )
