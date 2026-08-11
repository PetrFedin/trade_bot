from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_selection import (
    CrossSectionalSelection,
    SelectionCandidate,
)


class RankedSelectionProvider(Protocol):
    top_k: int

    def select(self, bars: tuple[OhlcvBar, ...]) -> CrossSectionalSelection: ...


class EntryQualityBlockReason(StrEnum):
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    TREND_EFFICIENCY_BELOW_MINIMUM = "TREND_EFFICIENCY_BELOW_MINIMUM"
    PRICE_EXTENSION_ABOVE_MAXIMUM = "PRICE_EXTENSION_ABOVE_MAXIMUM"
    SINGLE_BAR_RETURN_ABOVE_MAXIMUM = "SINGLE_BAR_RETURN_ABOVE_MAXIMUM"
    AVERAGE_DOLLAR_VOLUME_BELOW_MINIMUM = "AVERAGE_DOLLAR_VOLUME_BELOW_MINIMUM"


@dataclass(frozen=True)
class EntryQualityPolicy:
    lookback_bars: int = 8
    minimum_trend_efficiency: Decimal = Decimal("0.35")
    maximum_price_extension_fraction: Decimal = Decimal("0.04")
    maximum_single_bar_return_fraction: Decimal = Decimal("0.05")
    minimum_average_dollar_volume: Decimal | None = None

    def validate(self) -> None:
        if self.lookback_bars < 3:
            raise ValueError("lookback_bars must be at least 3")
        if (
            not self.minimum_trend_efficiency.is_finite()
            or self.minimum_trend_efficiency < 0
            or self.minimum_trend_efficiency > 1
        ):
            raise ValueError("minimum_trend_efficiency must be within [0, 1]")
        for field_name, value in (
            ("maximum_price_extension_fraction", self.maximum_price_extension_fraction),
            ("maximum_single_bar_return_fraction", self.maximum_single_bar_return_fraction),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")
        if self.minimum_average_dollar_volume is not None and (
            not self.minimum_average_dollar_volume.is_finite()
            or self.minimum_average_dollar_volume <= 0
        ):
            raise ValueError("minimum_average_dollar_volume must be positive and finite")


@dataclass(frozen=True)
class EntryQualityMetrics:
    symbol: str
    trend_efficiency: Decimal | None
    price_extension_fraction: Decimal | None
    single_bar_return_fraction: Decimal | None
    average_dollar_volume: Decimal | None


@dataclass(frozen=True)
class EntryQualityEvaluation:
    symbol: str
    passed: bool
    selected: bool
    metrics: EntryQualityMetrics
    block_reasons: tuple[EntryQualityBlockReason, ...]


@dataclass(frozen=True)
class EntryQualitySelectionTrace:
    base_selected_symbols: tuple[str, ...]
    selected_symbols: tuple[str, ...]
    evaluations: tuple[EntryQualityEvaluation, ...]

    @property
    def blocked_symbols(self) -> tuple[str, ...]:
        return tuple(
            evaluation.symbol
            for evaluation in self.evaluations
            if not evaluation.passed
        )


class EntryQualityFilteredSelector:
    """Shadow wrapper that rejects choppy or late-spike entry candidates.

    The wrapper never changes the base candidate score. It walks the base selector's
    eligible ranking and admits the first ``top_k`` candidates that pass transparent
    path-quality checks. This makes attribution explicit: a symbol can be high-ranked
    by momentum/trend/volatility yet still be rejected as a poor *entry*.
    """

    def __init__(
        self,
        *,
        base_selector: RankedSelectionProvider,
        policy: EntryQualityPolicy | None = None,
    ) -> None:
        resolved = EntryQualityPolicy() if policy is None else policy
        resolved.validate()
        if base_selector.top_k < 1:
            raise ValueError("base selector top_k must be positive")
        self.base_selector = base_selector
        self.policy = resolved
        self.top_k = base_selector.top_k

    @property
    def signal_config(self):
        return getattr(self.base_selector, "signal_config", None)

    def select(self, bars) -> CrossSectionalSelection:
        selection, _ = self.select_with_trace(bars)
        return selection

    def select_with_trace(
        self,
        bars,
    ) -> tuple[CrossSectionalSelection, EntryQualitySelectionTrace]:
        materialized = tuple(bars)
        base = self.base_selector.select(materialized)
        by_symbol: dict[str, list[OhlcvBar]] = {}
        for bar in materialized:
            by_symbol.setdefault(bar.symbol, []).append(bar)
        for symbol_bars in by_symbol.values():
            symbol_bars.sort(key=lambda bar: bar.timestamp)

        selected: list[str] = []
        evaluations: list[EntryQualityEvaluation] = []
        for candidate in self._ranked_eligible(base.candidates):
            metrics, reasons = self._evaluate(
                candidate.symbol,
                by_symbol.get(candidate.symbol, []),
            )
            passed = not reasons
            chosen = passed and len(selected) < self.top_k
            if chosen:
                selected.append(candidate.symbol)
            evaluations.append(
                EntryQualityEvaluation(
                    symbol=candidate.symbol,
                    passed=passed,
                    selected=chosen,
                    metrics=metrics,
                    block_reasons=reasons,
                )
            )

        filtered = CrossSectionalSelection(
            decision_time=base.decision_time,
            selected_symbols=tuple(selected),
            candidates=base.candidates,
        )
        return filtered, EntryQualitySelectionTrace(
            base_selected_symbols=base.selected_symbols,
            selected_symbols=filtered.selected_symbols,
            evaluations=tuple(evaluations),
        )

    @staticmethod
    def _ranked_eligible(
        candidates: tuple[SelectionCandidate, ...],
    ) -> tuple[SelectionCandidate, ...]:
        indexed = tuple(enumerate(candidate for candidate in candidates if candidate.eligible))
        ranked = sorted(
            indexed,
            key=lambda item: (
                item[1].rank is None,
                item[1].rank if item[1].rank is not None else 10**9,
                item[0],
            ),
        )
        return tuple(candidate for _, candidate in ranked)

    def _evaluate(
        self,
        symbol: str,
        bars: list[OhlcvBar],
    ) -> tuple[EntryQualityMetrics, tuple[EntryQualityBlockReason, ...]]:
        if len(bars) < self.policy.lookback_bars:
            return (
                EntryQualityMetrics(
                    symbol=symbol,
                    trend_efficiency=None,
                    price_extension_fraction=None,
                    single_bar_return_fraction=None,
                    average_dollar_volume=None,
                ),
                (EntryQualityBlockReason.INSUFFICIENT_HISTORY,),
            )
        window = bars[-self.policy.lookback_bars :]
        closes = tuple(bar.close for bar in window)
        path_length = sum(
            (abs(current - prior) for prior, current in zip(closes, closes[1:])),
            start=Decimal("0"),
        )
        net_change = abs(closes[-1] - closes[0])
        trend_efficiency = (
            Decimal("0") if path_length == 0 else net_change / path_length
        )
        mean_close = sum(closes, start=Decimal("0")) / Decimal(len(closes))
        price_extension = max(Decimal("0"), closes[-1] / mean_close - Decimal("1"))
        single_bar_return = max(
            Decimal("0"),
            closes[-1] / closes[-2] - Decimal("1"),
        )
        average_dollar_volume = sum(
            (bar.close * Decimal(bar.volume) for bar in window),
            start=Decimal("0"),
        ) / Decimal(len(window))

        reasons: list[EntryQualityBlockReason] = []
        if trend_efficiency < self.policy.minimum_trend_efficiency:
            reasons.append(EntryQualityBlockReason.TREND_EFFICIENCY_BELOW_MINIMUM)
        if price_extension > self.policy.maximum_price_extension_fraction:
            reasons.append(EntryQualityBlockReason.PRICE_EXTENSION_ABOVE_MAXIMUM)
        if single_bar_return > self.policy.maximum_single_bar_return_fraction:
            reasons.append(EntryQualityBlockReason.SINGLE_BAR_RETURN_ABOVE_MAXIMUM)
        minimum_liquidity = self.policy.minimum_average_dollar_volume
        if minimum_liquidity is not None and average_dollar_volume < minimum_liquidity:
            reasons.append(EntryQualityBlockReason.AVERAGE_DOLLAR_VOLUME_BELOW_MINIMUM)
        return (
            EntryQualityMetrics(
                symbol=symbol,
                trend_efficiency=trend_efficiency,
                price_extension_fraction=price_extension,
                single_bar_return_fraction=single_bar_return,
                average_dollar_volume=average_dollar_volume,
            ),
            tuple(reasons),
        )
