from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class CryptoMarketHistoryPolicy:
    fast_ema_bars: int = 21
    slow_ema_bars: int = 55
    momentum_bars: int = 12
    atr_bars: int = 14
    minimum_regime_episode_bars: int = 6
    minimum_correlation_observations: int = 100

    def validate(self) -> None:
        if self.fast_ema_bars < 2 or self.slow_ema_bars <= self.fast_ema_bars:
            raise ValueError("crypto history EMA windows are invalid")
        if self.momentum_bars < 1 or self.atr_bars < 2:
            raise ValueError("crypto history momentum/ATR windows must be positive")
        if self.minimum_regime_episode_bars < 1:
            raise ValueError("crypto history minimum episode bars must be positive")
        if self.minimum_correlation_observations < 3:
            raise ValueError("crypto history correlation observations must be at least 3")


@dataclass(frozen=True)
class CryptoRegimePoint:
    symbol: str
    time: str
    close: Decimal
    fast_ema: Decimal
    slow_ema: Decimal
    momentum: Decimal
    atr_fraction: Decimal
    trend_regime: str


@dataclass(frozen=True)
class CryptoRegimeEpisode:
    symbol: str
    regime: str
    start_time: str
    end_time: str
    bar_count: int
    return_fraction: Decimal


def profile_crypto_market_history(
    acquisition: BybitKlineAcquisition,
    *,
    policy: CryptoMarketHistoryPolicy | None = None,
    interval: str = "60",
) -> dict[str, Any]:
    """Describe full-period market regimes and cross-symbol return relationships.

    This is descriptive research. Regime labels use only information available at each bar, while
    aggregate episode/correlation summaries naturally use the completed historical sample. Nothing
    in this report changes trading thresholds or grants activation.
    """

    active = CryptoMarketHistoryPolicy() if policy is None else policy
    active.validate()
    bars_by_symbol = _bars_by_symbol(acquisition.bars)
    if len(bars_by_symbol) < 2:
        raise ValueError("crypto full-history profile requires at least two symbols")

    symbol_profiles: dict[str, dict[str, Any]] = {}
    regimes_by_symbol: dict[str, tuple[CryptoRegimePoint, ...]] = {}
    episodes: list[CryptoRegimeEpisode] = []
    returns_by_symbol: dict[str, dict[str, Decimal]] = {}
    for symbol, bars in sorted(bars_by_symbol.items()):
        if len(bars) < active.slow_ema_bars + active.momentum_bars:
            raise ValueError(f"crypto full-history profile has insufficient bars:{symbol}")
        regimes = _regime_points(symbol, bars, policy=active)
        regimes_by_symbol[symbol] = regimes
        symbol_episodes = _episodes(regimes, policy=active)
        episodes.extend(symbol_episodes)
        returns = _returns_by_time(bars)
        returns_by_symbol[symbol] = returns
        symbol_profiles[symbol] = _symbol_profile(
            bars,
            regimes=regimes,
            episodes=symbol_episodes,
        )

    correlations = _pairwise_correlations(
        returns_by_symbol,
        minimum_observations=active.minimum_correlation_observations,
    )
    return {
        "diagnostic": "BYBIT_CRYPTO_FULL_HISTORY_MARKET_PROFILE",
        "interval": interval,
        "symbol_count": len(symbol_profiles),
        "symbols": sorted(symbol_profiles),
        "policy": {
            "fast_ema_bars": active.fast_ema_bars,
            "slow_ema_bars": active.slow_ema_bars,
            "momentum_bars": active.momentum_bars,
            "atr_bars": active.atr_bars,
            "minimum_regime_episode_bars": active.minimum_regime_episode_bars,
            "minimum_correlation_observations": active.minimum_correlation_observations,
        },
        "symbol_profiles": symbol_profiles,
        "regime_episode_summary": _episode_summary(episodes),
        "pairwise_return_correlations": correlations,
        "highest_positive_correlations": sorted(
            correlations,
            key=lambda item: (-float(item["correlation"]), item["pair"]),
        )[:10],
        "lowest_correlations": sorted(
            correlations,
            key=lambda item: (float(item["correlation"]), item["pair"]),
        )[:10],
        "regime_label_timing": (
            "each label uses same-bar completed close plus recursively available EMA, momentum and ATR"
        ),
        "interpretation_contract": (
            "descriptive completed-history relationships only; correlation and repeated regimes do "
            "not establish causality or guarantee future returns"
        ),
        "strategy_parameters_changed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _regime_points(
    symbol: str,
    bars: Sequence[BybitKlineBar],
    *,
    policy: CryptoMarketHistoryPolicy,
) -> tuple[CryptoRegimePoint, ...]:
    fast_alpha = Decimal("2") / Decimal(policy.fast_ema_bars + 1)
    slow_alpha = Decimal("2") / Decimal(policy.slow_ema_bars + 1)
    fast = bars[0].close
    slow = bars[0].close
    true_ranges: list[Decimal] = []
    result: list[CryptoRegimePoint] = []
    previous_close = bars[0].close
    for index, bar in enumerate(bars):
        bar.validate()
        if index > 0:
            fast = bar.close * fast_alpha + fast * (_ONE - fast_alpha)
            slow = bar.close * slow_alpha + slow * (_ONE - slow_alpha)
        true_range = max(
            bar.high - bar.low,
            abs(bar.high - previous_close),
            abs(bar.low - previous_close),
        )
        true_ranges.append(true_range)
        previous_close = bar.close
        if index < max(policy.slow_ema_bars - 1, policy.momentum_bars, policy.atr_bars - 1):
            continue
        atr = sum(true_ranges[-policy.atr_bars :], start=_ZERO) / Decimal(policy.atr_bars)
        if atr <= 0:
            raise ValueError("crypto full-history ATR must be positive")
        momentum = bar.close / bars[index - policy.momentum_bars].close - _ONE
        if bar.close > fast > slow and momentum > 0:
            regime = "BULL_TREND"
        elif bar.close < fast < slow and momentum < 0:
            regime = "BEAR_TREND"
        else:
            regime = "RANGE_TRANSITION"
        result.append(
            CryptoRegimePoint(
                symbol=symbol,
                time=bar.start_time.isoformat(),
                close=bar.close,
                fast_ema=fast,
                slow_ema=slow,
                momentum=momentum,
                atr_fraction=atr / bar.close,
                trend_regime=regime,
            )
        )
    if not result:
        raise ValueError("crypto full-history regime reconstruction produced no points")
    return tuple(result)


def _episodes(
    points: Sequence[CryptoRegimePoint],
    *,
    policy: CryptoMarketHistoryPolicy,
) -> tuple[CryptoRegimeEpisode, ...]:
    if not points:
        return ()
    groups: list[list[CryptoRegimePoint]] = []
    active: list[CryptoRegimePoint] = [points[0]]
    for point in points[1:]:
        if point.trend_regime == active[-1].trend_regime:
            active.append(point)
        else:
            groups.append(active)
            active = [point]
    groups.append(active)
    episodes: list[CryptoRegimeEpisode] = []
    for group in groups:
        if len(group) < policy.minimum_regime_episode_bars:
            continue
        episodes.append(
            CryptoRegimeEpisode(
                symbol=group[0].symbol,
                regime=group[0].trend_regime,
                start_time=group[0].time,
                end_time=group[-1].time,
                bar_count=len(group),
                return_fraction=group[-1].close / group[0].close - _ONE,
            )
        )
    return tuple(episodes)


def _symbol_profile(
    bars: Sequence[BybitKlineBar],
    *,
    regimes: Sequence[CryptoRegimePoint],
    episodes: Sequence[CryptoRegimeEpisode],
) -> dict[str, Any]:
    closes = [bar.close for bar in bars]
    returns = [closes[index] / closes[index - 1] - _ONE for index in range(1, len(closes))]
    total_return = closes[-1] / closes[0] - _ONE
    max_drawdown = _maximum_drawdown(closes)
    mean_return = sum(returns, start=_ZERO) / Decimal(len(returns))
    mean_abs_return = sum((abs(value) for value in returns), start=_ZERO) / Decimal(len(returns))
    volatility = _population_stddev(returns)
    regime_counts = Counter(point.trend_regime for point in regimes)
    episode_counts = Counter(episode.regime for episode in episodes)
    current = regimes[-1]
    return {
        "bar_count": len(bars),
        "first_bar": bars[0].start_time.isoformat(),
        "last_bar": bars[-1].start_time.isoformat(),
        "first_close": float(closes[0]),
        "last_close": float(closes[-1]),
        "total_return_fraction": float(total_return),
        "maximum_drawdown_fraction": float(max_drawdown),
        "mean_bar_return_fraction": float(mean_return),
        "mean_absolute_bar_return_fraction": float(mean_abs_return),
        "bar_return_stddev_fraction": volatility,
        "positive_bar_fraction": float(
            Decimal(sum(value > 0 for value in returns)) / Decimal(len(returns))
        ),
        "regime_bar_counts": dict(sorted(regime_counts.items())),
        "qualifying_episode_counts": dict(sorted(episode_counts.items())),
        "current_regime": current.trend_regime,
        "current_momentum_fraction": float(current.momentum),
        "current_atr_fraction": float(current.atr_fraction),
        "current_fast_ema": float(current.fast_ema),
        "current_slow_ema": float(current.slow_ema),
    }


def _episode_summary(episodes: Sequence[CryptoRegimeEpisode]) -> dict[str, Any]:
    grouped: dict[str, list[CryptoRegimeEpisode]] = defaultdict(list)
    for episode in episodes:
        grouped[episode.regime].append(episode)
    result: dict[str, Any] = {}
    for regime, members in sorted(grouped.items()):
        returns = [episode.return_fraction for episode in members]
        bars = [episode.bar_count for episode in members]
        result[regime] = {
            "episode_count": len(members),
            "average_episode_return_fraction": float(
                sum(returns, start=_ZERO) / Decimal(len(returns))
            ),
            "positive_episode_fraction": float(
                Decimal(sum(value > 0 for value in returns)) / Decimal(len(returns))
            ),
            "average_episode_bars": float(Decimal(sum(bars)) / Decimal(len(bars))),
            "maximum_episode_bars": max(bars),
        }
    return result


def _pairwise_correlations(
    returns_by_symbol: Mapping[str, Mapping[str, Decimal]],
    *,
    minimum_observations: int,
) -> list[dict[str, Any]]:
    symbols = sorted(returns_by_symbol)
    result: list[dict[str, Any]] = []
    for index, left in enumerate(symbols):
        for right in symbols[index + 1 :]:
            common = sorted(set(returns_by_symbol[left]) & set(returns_by_symbol[right]))
            if len(common) < minimum_observations:
                continue
            left_values = [returns_by_symbol[left][time] for time in common]
            right_values = [returns_by_symbol[right][time] for time in common]
            correlation = _pearson(left_values, right_values)
            if correlation is None:
                continue
            result.append(
                {
                    "pair": f"{left}:{right}",
                    "left_symbol": left,
                    "right_symbol": right,
                    "observation_count": len(common),
                    "correlation": correlation,
                }
            )
    return sorted(result, key=lambda item: item["pair"])


def _returns_by_time(bars: Sequence[BybitKlineBar]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for previous, current in zip(bars, bars[1:], strict=False):
        result[current.start_time.isoformat()] = current.close / previous.close - _ONE
    return result


def _maximum_drawdown(closes: Sequence[Decimal]) -> Decimal:
    peak = closes[0]
    maximum = _ZERO
    for close in closes:
        peak = max(peak, close)
        drawdown = (peak - close) / peak
        maximum = max(maximum, drawdown)
    return maximum


def _population_stddev(values: Sequence[Decimal]) -> float:
    if not values:
        return 0.0
    mean = sum(values, start=_ZERO) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), start=_ZERO) / Decimal(len(values))
    return math.sqrt(float(variance))


def _pearson(left: Sequence[Decimal], right: Sequence[Decimal]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        raise ValueError("crypto history correlation inputs must have equal length >= 3")
    left_mean = sum(left, start=_ZERO) / Decimal(len(left))
    right_mean = sum(right, start=_ZERO) / Decimal(len(right))
    numerator = sum(
        ((lvalue - left_mean) * (rvalue - right_mean) for lvalue, rvalue in zip(left, right, strict=True)),
        start=_ZERO,
    )
    left_ss = sum(((value - left_mean) ** 2 for value in left), start=_ZERO)
    right_ss = sum(((value - right_mean) ** 2 for value in right), start=_ZERO)
    if left_ss <= 0 or right_ss <= 0:
        return None
    return float(numerator / (left_ss * right_ss).sqrt())


def _bars_by_symbol(
    bars: Sequence[BybitKlineBar],
) -> dict[str, tuple[BybitKlineBar, ...]]:
    grouped: dict[str, list[BybitKlineBar]] = defaultdict(list)
    for bar in bars:
        bar.validate()
        grouped[bar.symbol].append(bar)
    result: dict[str, tuple[BybitKlineBar, ...]] = {}
    for symbol, values in grouped.items():
        ordered = tuple(sorted(values, key=lambda item: item.start_time))
        if len({bar.start_time for bar in ordered}) != len(ordered):
            raise ValueError("crypto full-history profile has duplicate symbol/timestamp")
        result[symbol] = ordered
    return result
