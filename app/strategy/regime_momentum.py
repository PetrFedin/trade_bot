from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from app.domain.trading import Bar, TargetPosition
from app.strategy.momentum import LongOnlyMomentumStrategy


@dataclass(frozen=True)
class RegimeAwareMomentumConfig:
    fast_bars: int = 3
    slow_bars: int = 8
    momentum_lookback_bars: int = 3
    volatility_bars: int = 5
    minimum_momentum_return: Decimal = Decimal("0.002")
    minimum_trend_strength: Decimal = Decimal("0.001")
    maximum_realized_volatility: Decimal = Decimal("0.03")

    def validate(self) -> None:
        if self.fast_bars < 2:
            raise ValueError("fast_bars must be at least two")
        if self.slow_bars <= self.fast_bars:
            raise ValueError("slow_bars must exceed fast_bars")
        if self.momentum_lookback_bars < 1:
            raise ValueError("momentum_lookback_bars must be positive")
        if self.volatility_bars < 2:
            raise ValueError("volatility_bars must be at least two")
        for name, value in (
            ("minimum_momentum_return", self.minimum_momentum_return),
            ("minimum_trend_strength", self.minimum_trend_strength),
            ("maximum_realized_volatility", self.maximum_realized_volatility),
        ):
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        if self.minimum_momentum_return < 0:
            raise ValueError("minimum_momentum_return must be non-negative")
        if self.minimum_trend_strength < 0:
            raise ValueError("minimum_trend_strength must be non-negative")
        if self.maximum_realized_volatility <= 0:
            raise ValueError("maximum_realized_volatility must be positive")

    @property
    def minimum_history_bars(self) -> int:
        return max(
            self.slow_bars,
            self.momentum_lookback_bars + 1,
            self.volatility_bars + 1,
        )


@dataclass(frozen=True)
class MomentumSignal:
    eligible: bool
    reasons: tuple[str, ...]
    fast_average: Decimal
    slow_average: Decimal
    momentum_return: Decimal
    trend_strength: Decimal
    realized_volatility: Decimal


class RegimeAwareMomentumStrategy(LongOnlyMomentumStrategy):
    """Close-only shadow strategy with trend, momentum and volatility confirmation.

    The strategy is intentionally paper/backtest-only until independent historical and
    external Paper evidence supports promotion. It does not claim profitable outcomes.
    """

    def __init__(
        self,
        *,
        strategy_id: str = "paper-regime-momentum-shadow-v1",
        target_quantity: Decimal = Decimal("1"),
        config: RegimeAwareMomentumConfig | None = None,
    ) -> None:
        super().__init__(strategy_id=strategy_id, target_quantity=target_quantity)
        self.config = RegimeAwareMomentumConfig() if config is None else config
        self.config.validate()

    def signal(self, bars: Sequence[Bar]) -> MomentumSignal:
        if len(bars) < self.config.minimum_history_bars:
            raise ValueError(
                f"at least {self.config.minimum_history_bars} bars are required"
            )
        for bar in bars:
            bar.validate()
        symbols = {bar.symbol for bar in bars}
        if len(symbols) != 1:
            raise ValueError("strategy input must contain exactly one symbol")
        ordered = sorted(bars, key=lambda value: value.timestamp)
        if len({bar.timestamp for bar in ordered}) != len(ordered):
            raise ValueError("duplicate bar timestamps are forbidden")

        fast = ordered[-self.config.fast_bars :]
        slow = ordered[-self.config.slow_bars :]
        fast_average = _mean_close(fast)
        slow_average = _mean_close(slow)
        latest = ordered[-1]
        momentum_reference = ordered[-1 - self.config.momentum_lookback_bars]
        momentum_return = latest.close / momentum_reference.close - Decimal("1")
        trend_strength = fast_average / slow_average - Decimal("1")
        volatility = _realized_volatility(
            ordered[-(self.config.volatility_bars + 1) :]
        )

        reasons: list[str] = []
        if latest.close <= fast_average:
            reasons.append("PRICE_NOT_ABOVE_FAST_AVERAGE")
        if fast_average <= slow_average:
            reasons.append("FAST_AVERAGE_NOT_ABOVE_SLOW")
        if momentum_return < self.config.minimum_momentum_return:
            reasons.append("MOMENTUM_BELOW_MINIMUM")
        if trend_strength < self.config.minimum_trend_strength:
            reasons.append("TREND_STRENGTH_BELOW_MINIMUM")
        if volatility > self.config.maximum_realized_volatility:
            reasons.append("REALIZED_VOLATILITY_ABOVE_LIMIT")
        return MomentumSignal(
            eligible=not reasons,
            reasons=tuple(reasons),
            fast_average=fast_average,
            slow_average=slow_average,
            momentum_return=momentum_return,
            trend_strength=trend_strength,
            realized_volatility=volatility,
        )

    def target(self, bars: Sequence[Bar]) -> TargetPosition:
        signal = self.signal(bars)
        ordered = sorted(bars, key=lambda value: value.timestamp)
        latest = ordered[-1]
        return TargetPosition(
            symbol=latest.symbol,
            quantity=self.target_quantity if signal.eligible else Decimal("0"),
            reference_price=latest.close,
            generated_at=latest.timestamp,
            strategy_id=self.strategy_id,
        )


def _mean_close(bars: Sequence[Bar]) -> Decimal:
    return sum((bar.close for bar in bars), Decimal("0")) / Decimal(len(bars))


def _realized_volatility(bars: Sequence[Bar]) -> Decimal:
    returns = [
        current.close / prior.close - Decimal("1") for prior, current in pairwise(bars)
    ]
    mean = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum(((value - mean) ** 2 for value in returns), Decimal("0")) / Decimal(
        len(returns)
    )
    return variance.sqrt()
