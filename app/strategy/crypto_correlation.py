from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from app.marketdata.bybit_v5 import BybitKlineBar


@dataclass(frozen=True)
class CryptoCorrelationPolicy:
    """Shadow-only positive-correlation concentration guard for concurrent crypto positions."""

    lookback_bars: int = 20
    minimum_return_observations: int = 10
    maximum_pairwise_correlation: Decimal = Decimal("0.85")

    def validate(self) -> None:
        if self.lookback_bars < 2:
            raise ValueError("crypto correlation lookback must be at least two bars")
        if self.minimum_return_observations < 2:
            raise ValueError("crypto correlation observations must be at least two")
        if self.minimum_return_observations > self.lookback_bars:
            raise ValueError("crypto correlation observations cannot exceed lookback")
        if (
            not self.maximum_pairwise_correlation.is_finite()
            or self.maximum_pairwise_correlation <= 0
            or self.maximum_pairwise_correlation > 1
        ):
            raise ValueError("crypto correlation ceiling must be finite and within (0, 1]")


@dataclass(frozen=True)
class CryptoCorrelationDecision:
    eligible: bool
    reason: str | None
    blocking_symbol: str | None
    correlation: Decimal | None
    shadow_only: bool = True
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False


def evaluate_crypto_correlation(
    candidate_symbol: str,
    *,
    selected_symbols: Sequence[str],
    histories: Mapping[str, Sequence[BybitKlineBar]],
    policy: CryptoCorrelationPolicy | None = None,
) -> CryptoCorrelationDecision:
    """Block a candidate when completed-bar returns duplicate an existing exposure.

    Only positive correlation above the predeclared ceiling is blocked. Negative correlation is
    not penalized. If a peer exists but synchronized history is insufficient, the shadow gate
    fails closed rather than pretending diversification was verified.
    """

    active = CryptoCorrelationPolicy() if policy is None else policy
    active.validate()
    if candidate_symbol not in histories:
        raise ValueError("crypto correlation candidate history is missing")
    if not selected_symbols:
        return CryptoCorrelationDecision(True, None, None, None)

    candidate = histories[candidate_symbol]
    for selected_symbol in selected_symbols:
        if selected_symbol == candidate_symbol:
            continue
        selected = histories.get(selected_symbol)
        if selected is None:
            return CryptoCorrelationDecision(
                False,
                "CORRELATION_HISTORY_INSUFFICIENT",
                selected_symbol,
                None,
            )
        try:
            correlation = crypto_return_correlation(
                candidate,
                selected,
                lookback_bars=active.lookback_bars,
                minimum_return_observations=active.minimum_return_observations,
            )
        except ValueError as exc:
            if "insufficient" not in str(exc).lower():
                raise
            return CryptoCorrelationDecision(
                False,
                "CORRELATION_HISTORY_INSUFFICIENT",
                selected_symbol,
                None,
            )
        if correlation > active.maximum_pairwise_correlation:
            return CryptoCorrelationDecision(
                False,
                "PAIRWISE_CORRELATION_ABOVE_LIMIT",
                selected_symbol,
                correlation,
            )
    return CryptoCorrelationDecision(True, None, None, None)


def crypto_return_correlation(
    left: Sequence[BybitKlineBar],
    right: Sequence[BybitKlineBar],
    *,
    lookback_bars: int,
    minimum_return_observations: int,
) -> Decimal:
    if lookback_bars < 2:
        raise ValueError("crypto correlation lookback must be at least two bars")
    if minimum_return_observations < 2:
        raise ValueError("crypto correlation observations must be at least two")
    if minimum_return_observations > lookback_bars:
        raise ValueError("crypto correlation observations cannot exceed lookback")

    left_by_time = {bar.start_time: bar.close for bar in left}
    right_by_time = {bar.start_time: bar.close for bar in right}
    common = sorted(set(left_by_time) & set(right_by_time))
    required_prices = minimum_return_observations + 1
    if len(common) < required_prices:
        raise ValueError("insufficient synchronized history for crypto correlation")
    timestamps = common[-(lookback_bars + 1) :]
    if len(timestamps) < required_prices:
        raise ValueError("insufficient crypto correlation observations after lookback")

    left_returns = [
        left_by_time[current] / left_by_time[previous] - Decimal("1")
        for previous, current in pairwise(timestamps)
    ]
    right_returns = [
        right_by_time[current] / right_by_time[previous] - Decimal("1")
        for previous, current in pairwise(timestamps)
    ]
    return _pearson(left_returns, right_returns)


def _pearson(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("crypto correlation inputs must have equal length of at least two")
    count = Decimal(len(left))
    left_mean = sum(left, Decimal("0")) / count
    right_mean = sum(right, Decimal("0")) / count
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    covariance = sum(
        (
            left_value * right_value
            for left_value, right_value in zip(left_centered, right_centered, strict=True)
        ),
        Decimal("0"),
    ) / count
    left_variance = sum(
        (value * value for value in left_centered),
        Decimal("0"),
    ) / count
    right_variance = sum(
        (value * value for value in right_centered),
        Decimal("0"),
    ) / count
    if left_variance == 0 or right_variance == 0:
        return Decimal("0")
    correlation = covariance / (left_variance.sqrt() * right_variance.sqrt())
    return max(Decimal("-1"), min(Decimal("1"), correlation))