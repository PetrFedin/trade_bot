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
class SelectionCandidate:
    rank: int | None
    symbol: str
    eligible: bool
    rejection_reasons: tuple[str, ...]
    momentum_return: Decimal
    trend_strength: Decimal
    realized_volatility: Decimal
    reference_price: Decimal


@dataclass(frozen=True)
class CrossSectionalSelection:
    decision_time: datetime
    selected_symbols: tuple[str, ...]
    candidates: tuple[SelectionCandidate, ...]


class CrossSectionalSelector:
    """Shadow-only selector reusing the qualified regime-aware signal.

    Eligible symbols are ordered without fitted score weights: higher momentum,
    then higher trend strength, then lower realized volatility, then symbol as a
    deterministic tie-break. Ineligible symbols remain auditable but cannot be
    selected.
    """

    def __init__(
        self,
        *,
        top_k: int = 2,
        signal_config: RegimeAwareMomentumConfig | None = None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        self.top_k = top_k
        self.signal_config = (
            RegimeAwareMomentumConfig() if signal_config is None else signal_config
        )
        self.signal_config.validate()

    def select(self, bars: Iterable[OhlcvBar]) -> CrossSectionalSelection:
        grouped: dict[str, list[OhlcvBar]] = defaultdict(list)
        for bar in bars:
            bar.validate()
            grouped[bar.symbol].append(bar)
        if len(grouped) < 2:
            raise ValueError("cross-sectional selection requires at least two symbols")

        signals: dict[str, tuple[MomentumSignal, Decimal, datetime]] = {}
        decision_times: set[datetime] = set()
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
            decision_time = ordered[-1].timestamp
            decision_times.add(decision_time)
            signals[symbol] = (signal, ordered[-1].close, decision_time)
        if len(decision_times) != 1:
            raise ValueError("cross-sectional symbols must share the same decision timestamp")

        eligible = [
            (symbol, signal, price)
            for symbol, (signal, price, _) in signals.items()
            if signal.eligible
        ]
        eligible.sort(
            key=lambda item: (
                -item[1].momentum_return,
                -item[1].trend_strength,
                item[1].realized_volatility,
                item[0],
            )
        )
        rank_by_symbol = {
            symbol: index + 1 for index, (symbol, _, _) in enumerate(eligible)
        }
        selected = tuple(symbol for symbol, _, _ in eligible[: self.top_k])
        candidates = tuple(
            SelectionCandidate(
                rank=rank_by_symbol.get(symbol),
                symbol=symbol,
                eligible=signal.eligible,
                rejection_reasons=signal.reasons,
                momentum_return=signal.momentum_return,
                trend_strength=signal.trend_strength,
                realized_volatility=signal.realized_volatility,
                reference_price=price,
            )
            for symbol, (signal, price, _) in sorted(signals.items())
        )
        return CrossSectionalSelection(
            decision_time=next(iter(decision_times)),
            selected_symbols=selected,
            candidates=candidates,
        )
