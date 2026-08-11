from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.trading import Bar
from app.marketdata.ohlcv import OhlcvBar
from app.strategy.regime_momentum import (
    MomentumSignal,
    RegimeAwareMomentumConfig,
    RegimeAwareMomentumStrategy,
)


@dataclass(frozen=True)
class SelectionQualityPolicy:
    momentum_weight: Decimal = Decimal("1")
    trend_weight: Decimal = Decimal("1")
    volatility_penalty_weight: Decimal = Decimal("1")
    minimum_quality_score: Decimal | None = None

    def validate(self) -> None:
        for name, value in (
            ("momentum_weight", self.momentum_weight),
            ("trend_weight", self.trend_weight),
            ("volatility_penalty_weight", self.volatility_penalty_weight),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.momentum_weight + self.trend_weight <= 0:
            raise ValueError("at least one positive return-quality weight is required")
        if self.minimum_quality_score is not None and not self.minimum_quality_score.is_finite():
            raise ValueError("minimum_quality_score must be finite when supplied")

    def score(self, signal: MomentumSignal) -> Decimal:
        return (
            self.momentum_weight * signal.momentum_return
            + self.trend_weight * signal.trend_strength
            - self.volatility_penalty_weight * signal.realized_volatility
        )


@dataclass(frozen=True)
class SelectionCandidate:
    rank: int | None
    symbol: str
    eligible: bool
    rejection_reasons: tuple[str, ...]
    momentum_return: Decimal
    trend_strength: Decimal
    realized_volatility: Decimal
    quality_score: Decimal
    reference_price: Decimal


@dataclass(frozen=True)
class CrossSectionalSelection:
    decision_time: datetime
    selected_symbols: tuple[str, ...]
    candidates: tuple[SelectionCandidate, ...]


class CrossSectionalSelector:
    """Shadow-only selector reusing the qualified regime-aware signal.

    Without a quality policy, eligible symbols retain the qualified legacy ordering:
    higher momentum, then higher trend strength, then lower realized volatility. When
    an explicit quality policy is supplied, the same auditable signal components are
    combined into an unfitted linear quality score so risk can outweigh a marginal
    momentum advantage. Ineligible symbols remain auditable but cannot be selected.
    """

    def __init__(
        self,
        *,
        top_k: int = 2,
        signal_config: RegimeAwareMomentumConfig | None = None,
        quality_policy: SelectionQualityPolicy | None = None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        self.top_k = top_k
        self.signal_config = (
            RegimeAwareMomentumConfig() if signal_config is None else signal_config
        )
        self.signal_config.validate()
        if quality_policy is not None:
            quality_policy.validate()
        self.quality_policy = quality_policy

    def select(self, bars: Iterable[OhlcvBar]) -> CrossSectionalSelection:
        grouped: dict[str, list[OhlcvBar]] = defaultdict(list)
        for bar in bars:
            bar.validate()
            grouped[bar.symbol].append(bar)
        if len(grouped) < 2:
            raise ValueError("cross-sectional selection requires at least two symbols")

        signals: dict[str, tuple[MomentumSignal, Decimal, datetime, Decimal, tuple[str, ...]]] = {}
        decision_times: set[datetime] = set()
        scoring_policy = self.quality_policy or SelectionQualityPolicy()
        scoring_policy.validate()
        for symbol, symbol_bars in grouped.items():
            ordered = sorted(symbol_bars, key=lambda bar: bar.timestamp)
            if len(ordered) < self.signal_config.minimum_history_bars:
                raise ValueError(f"insufficient selection history:{symbol}")
            strategy_bars = [
                Bar(
                    symbol=symbol,
                    timestamp=bar.timestamp,
                    close=bar.close,
                )
                for bar in ordered
            ]
            strategy = RegimeAwareMomentumStrategy(
                strategy_id="cross-sectional-selection-shadow-v1",
                target_quantity=Decimal("1"),
                config=self.signal_config,
            )
            signal = strategy.signal(strategy_bars)
            quality_score = scoring_policy.score(signal)
            rejection_reasons = list(signal.reasons)
            if (
                self.quality_policy is not None
                and self.quality_policy.minimum_quality_score is not None
                and quality_score < self.quality_policy.minimum_quality_score
            ):
                rejection_reasons.append("QUALITY_SCORE_BELOW_MINIMUM")
            decision_time = ordered[-1].timestamp
            decision_times.add(decision_time)
            signals[symbol] = (
                signal,
                ordered[-1].close,
                decision_time,
                quality_score,
                tuple(rejection_reasons),
            )
        if len(decision_times) != 1:
            raise ValueError("cross-sectional symbols must share the same decision timestamp")

        eligible = [
            (symbol, signal, price, quality_score)
            for symbol, (signal, price, _, quality_score, reasons) in signals.items()
            if signal.eligible and "QUALITY_SCORE_BELOW_MINIMUM" not in reasons
        ]
        if self.quality_policy is None:
            eligible.sort(
                key=lambda item: (
                    -item[1].momentum_return,
                    -item[1].trend_strength,
                    item[1].realized_volatility,
                    item[0],
                )
            )
        else:
            eligible.sort(
                key=lambda item: (
                    -item[3],
                    -item[1].momentum_return,
                    -item[1].trend_strength,
                    item[1].realized_volatility,
                    item[0],
                )
            )
        rank_by_symbol = {
            symbol: index + 1 for index, (symbol, _, _, _) in enumerate(eligible)
        }
        selected = tuple(symbol for symbol, _, _, _ in eligible[: self.top_k])
        candidates = tuple(
            SelectionCandidate(
                rank=rank_by_symbol.get(symbol),
                symbol=symbol,
                eligible=(
                    signal.eligible and "QUALITY_SCORE_BELOW_MINIMUM" not in rejection_reasons
                ),
                rejection_reasons=rejection_reasons,
                momentum_return=signal.momentum_return,
                trend_strength=signal.trend_strength,
                realized_volatility=signal.realized_volatility,
                quality_score=quality_score,
                reference_price=price,
            )
            for symbol, (
                signal,
                price,
                _,
                quality_score,
                rejection_reasons,
            ) in sorted(signals.items())
        )
        return CrossSectionalSelection(
            decision_time=next(iter(decision_times)),
            selected_symbols=selected,
            candidates=candidates,
        )
