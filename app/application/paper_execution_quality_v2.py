from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.application.paper_execution_quality import (
    PaperExecutionQualityFill,
    SQLitePaperExecutionQualityStore,
)
from app.domain.trading import Fill, Side
from app.oms.protocols import OmsStore


class HistoricalLimitReference(Protocol):
    """Resolve the effective order limit that was active at one fill timestamp."""

    def limit_price_for_fill(
        self,
        intent_id: str,
        *,
        occurred_at: datetime,
        fallback: Decimal,
    ) -> Decimal: ...


class OriginalOrderLimitReference:
    """Stable fallback for orders without time-indexed replacement evidence."""

    def limit_price_for_fill(
        self,
        intent_id: str,
        *,
        occurred_at: datetime,
        fallback: Decimal,
    ) -> Decimal:
        del intent_id, occurred_at
        return fallback


class ReplayStablePaperExecutionQualityTracker:
    """Exact-fill slippage tracker that never rewrites a persisted fill baseline.

    A new fill is evaluated against a reference resolved for that fill's timestamp.
    A duplicate replay first validates the immutable fill economics against the already
    persisted observation and returns without consulting a possibly newer limit state.
    """

    def __init__(
        self,
        *,
        oms: OmsStore,
        store: SQLitePaperExecutionQualityStore,
        limit_reference: HistoricalLimitReference | None = None,
    ) -> None:
        self.oms = oms
        self.store = store
        self.limit_reference = (
            OriginalOrderLimitReference()
            if limit_reference is None
            else limit_reference
        )

    def observe_fill(self, fill: Fill) -> None:
        fill.validate()
        existing = self._existing(fill.fill_id)
        if existing is not None:
            self._validate_replay(existing, fill)
            return

        order = self.oms.get(fill.order_intent_id)
        if order is None:
            raise KeyError(fill.order_intent_id)
        if order.symbol != fill.symbol or order.side is not fill.side:
            raise ValueError("PAPER_EXECUTION_QUALITY_ORDER_IDENTITY_MISMATCH")
        expected = self.limit_reference.limit_price_for_fill(
            fill.order_intent_id,
            occurred_at=fill.occurred_at,
            fallback=order.limit_price,
        )
        if not expected.is_finite() or expected <= 0:
            raise ValueError("historical execution limit must be positive and finite")
        raw_fraction = (fill.price - expected) / expected
        signed_fraction = raw_fraction if fill.side is Side.BUY else -raw_fraction
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
                signed_slippage_notional=signed_fraction * expected * fill.quantity,
                occurred_at=fill.occurred_at,
            )
        )

    def _existing(self, fill_id: str) -> PaperExecutionQualityFill | None:
        return next(
            (
                observation
                for observation in self.store.fills()
                if observation.fill_id == fill_id
            ),
            None,
        )

    @staticmethod
    def _validate_replay(
        existing: PaperExecutionQualityFill,
        fill: Fill,
    ) -> None:
        immutable = (
            existing.intent_id,
            existing.symbol,
            existing.side,
            existing.quantity,
            existing.fill_price,
            existing.occurred_at,
        )
        incoming = (
            fill.order_intent_id,
            fill.symbol,
            fill.side,
            fill.quantity,
            fill.price,
            fill.occurred_at,
        )
        if immutable != incoming:
            raise ValueError("PAPER_EXECUTION_QUALITY_FILL_CONFLICT")
