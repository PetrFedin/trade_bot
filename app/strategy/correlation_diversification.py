from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_selection import (
    CrossSectionalSelection,
    CrossSectionalSelector,
    SelectionCandidate,
)
from app.strategy.regime_momentum import RegimeAwareMomentumConfig


@dataclass(frozen=True)
class CorrelationDiversificationPolicy:
    lookback_bars: int = 20
    minimum_return_observations: int = 10
    maximum_pairwise_correlation: Decimal = Decimal("0.85")

    def validate(self) -> None:
        if self.lookback_bars < 2:
            raise ValueError("lookback_bars must be at least two")
        if self.minimum_return_observations < 2:
            raise ValueError("minimum_return_observations must be at least two")
        if self.minimum_return_observations > self.lookback_bars:
            raise ValueError(
                "minimum_return_observations cannot exceed lookback_bars"
            )
        if (
            not self.maximum_pairwise_correlation.is_finite()
            or self.maximum_pairwise_correlation <= 0
            or self.maximum_pairwise_correlation > 1
        ):
            raise ValueError(
                "maximum_pairwise_correlation must be finite and within (0, 1]"
            )


@dataclass(frozen=True)
class DiversificationBlock:
    symbol: str
    selected_symbol: str
    correlation: Decimal


@dataclass(frozen=True)
class DiversifiedSelectionDecision:
    selection: CrossSectionalSelection
    blocks: tuple[DiversificationBlock, ...]


class DiversifiedCrossSectionalSelector:
    """Greedy correlation-aware wrapper around a qualified cross-sectional selector.

    The wrapped selector remains responsible for signal eligibility and ranking. This
    layer only prevents a lower-ranked candidate from consuming another top-K slot
    when its recent return correlation with an already selected symbol exceeds the
    predeclared limit. Negative correlation is not penalized.
    """

    def __init__(
        self,
        *,
        base_selector: CrossSectionalSelector,
        policy: CorrelationDiversificationPolicy | None = None,
    ) -> None:
        self.base_selector = base_selector
        self.policy = (
            CorrelationDiversificationPolicy() if policy is None else policy
        )
        self.policy.validate()

    @property
    def top_k(self) -> int:
        return self.base_selector.top_k

    @property
    def signal_config(self) -> RegimeAwareMomentumConfig:
        return self.base_selector.signal_config

    def select(self, bars: Iterable[OhlcvBar]) -> CrossSectionalSelection:
        return self.select_with_trace(bars).selection

    def select_with_trace(
        self, bars: Iterable[OhlcvBar]
    ) -> DiversifiedSelectionDecision:
        materialized = tuple(bars)
        base = self.base_selector.select(materialized)
        histories = _histories(materialized)
        ranked = sorted(
            (candidate for candidate in base.candidates if candidate.rank is not None),
            key=lambda candidate: candidate.rank or 0,
        )
        selected: list[str] = []
        blocks: list[DiversificationBlock] = []

        for candidate in ranked:
            if len(selected) >= self.top_k:
                break
            blocking = self._blocking_correlation(
                candidate=candidate,
                selected_symbols=tuple(selected),
                histories=histories,
            )
            if blocking is not None:
                blocks.append(blocking)
                continue
            selected.append(candidate.symbol)

        return DiversifiedSelectionDecision(
            selection=CrossSectionalSelection(
                decision_time=base.decision_time,
                selected_symbols=tuple(selected),
                candidates=base.candidates,
            ),
            blocks=tuple(blocks),
        )

    def _blocking_correlation(
        self,
        *,
        candidate: SelectionCandidate,
        selected_symbols: tuple[str, ...],
        histories: dict[str, tuple[OhlcvBar, ...]],
    ) -> DiversificationBlock | None:
        for selected_symbol in selected_symbols:
            correlation = pairwise_return_correlation(
                histories[candidate.symbol],
                histories[selected_symbol],
                lookback_bars=self.policy.lookback_bars,
                minimum_return_observations=self.policy.minimum_return_observations,
            )
            if correlation > self.policy.maximum_pairwise_correlation:
                return DiversificationBlock(
                    symbol=candidate.symbol,
                    selected_symbol=selected_symbol,
                    correlation=correlation,
                )
        return None


def _histories(bars: Iterable[OhlcvBar]) -> dict[str, tuple[OhlcvBar, ...]]:
    grouped: defaultdict[str, list[OhlcvBar]] = defaultdict(list)
    for bar in bars:
        bar.validate()
        grouped[bar.symbol].append(bar)
    return {
        symbol: tuple(sorted(values, key=lambda value: value.timestamp))
        for symbol, values in grouped.items()
    }


def pairwise_return_correlation(
    left: tuple[OhlcvBar, ...],
    right: tuple[OhlcvBar, ...],
    *,
    lookback_bars: int,
    minimum_return_observations: int,
) -> Decimal:
    if lookback_bars < 2:
        raise ValueError("lookback_bars must be at least two")
    if minimum_return_observations < 2:
        raise ValueError("minimum_return_observations must be at least two")
    if minimum_return_observations > lookback_bars:
        raise ValueError("minimum_return_observations cannot exceed lookback_bars")

    left_by_time = {bar.timestamp: bar.close for bar in left}
    right_by_time = {bar.timestamp: bar.close for bar in right}
    common = sorted(set(left_by_time) & set(right_by_time))
    required_prices = minimum_return_observations + 1
    if len(common) < required_prices:
        raise ValueError("insufficient common history for correlation diversification")
    timestamps = common[-(lookback_bars + 1) :]
    if len(timestamps) < required_prices:
        raise ValueError("insufficient correlation observations after lookback truncation")

    left_returns = [
        left_by_time[current] / left_by_time[prior] - Decimal("1")
        for prior, current in pairwise(timestamps)
    ]
    right_returns = [
        right_by_time[current] / right_by_time[prior] - Decimal("1")
        for prior, current in pairwise(timestamps)
    ]
    if len(left_returns) < minimum_return_observations:
        raise ValueError("insufficient paired return observations")
    return _pearson(left_returns, right_returns)


def _pearson(left: list[Decimal], right: list[Decimal]) -> Decimal:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("correlation inputs must have equal length of at least two")
    count = Decimal(len(left))
    left_mean = sum(left, Decimal("0")) / count
    right_mean = sum(right, Decimal("0")) / count
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    covariance = sum(
        (
            left_value * right_value
            for left_value, right_value in zip(
                left_centered,
                right_centered,
                strict=True,
            )
        ),
        Decimal("0"),
    ) / count
    left_variance = (
        sum((value * value for value in left_centered), Decimal("0")) / count
    )
    right_variance = (
        sum((value * value for value in right_centered), Decimal("0")) / count
    )
    if left_variance == 0 or right_variance == 0:
        return Decimal("0")
    correlation = covariance / (left_variance.sqrt() * right_variance.sqrt())
    return max(Decimal("-1"), min(Decimal("1"), correlation))
