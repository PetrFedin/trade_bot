from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.domain.trading import Bar, TargetPosition


class LongOnlyMomentumStrategy:
    """Deterministic validation strategy for the paper E2E vertical slice.

    This is not presented as a profitable strategy. It exists to exercise the complete
    data -> signal -> target -> risk -> order -> fill -> portfolio pipeline.
    """

    def __init__(self, *, strategy_id: str = "paper-momentum-v1", target_quantity: Decimal = Decimal("1")) -> None:
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        if not target_quantity.is_finite() or target_quantity <= 0:
            raise ValueError("target_quantity must be positive and finite")
        self.strategy_id = strategy_id
        self.target_quantity = target_quantity

    def target(self, bars: Sequence[Bar]) -> TargetPosition:
        if len(bars) < 3:
            raise ValueError("at least three bars are required")
        for bar in bars:
            bar.validate()
        symbols = {bar.symbol for bar in bars}
        if len(symbols) != 1:
            raise ValueError("strategy input must contain exactly one symbol")
        ordered = sorted(bars, key=lambda value: value.timestamp)
        if len({bar.timestamp for bar in ordered}) != len(ordered):
            raise ValueError("duplicate bar timestamps are forbidden")
        latest = ordered[-1]
        prior_average = sum((bar.close for bar in ordered[:-1]), Decimal("0")) / Decimal(len(ordered) - 1)
        quantity = self.target_quantity if latest.close > prior_average else Decimal("0")
        return TargetPosition(
            symbol=latest.symbol,
            quantity=quantity,
            reference_price=latest.close,
            generated_at=latest.timestamp,
            strategy_id=self.strategy_id,
        )
