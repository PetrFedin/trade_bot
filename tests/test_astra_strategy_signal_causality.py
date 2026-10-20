"""Research-only causality probes on ASTRA's actual shipped regime signal/selector.

No strategy parameters, risk authority, broker execution, or production files
are modified by these deterministic fixtures. A passing test covers only the
synthetic sequences shown here, not complete Strategy A lookahead qualification.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.trading import Bar
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy
from tests.test_cross_sectional_selection import symbol_bars
from tools.inspect_signal_lookahead import CloseBar, inspect_lookahead


def _history() -> list[CloseBar]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        CloseBar(start + timedelta(days=i), Decimal("100") + Decimal(i))
        for i in range(20)
    ]


def _actual_regime_signals(history: tuple[CloseBar, ...]) -> tuple[str, ...]:
    """Adapt the real regime signal to the diagnostics' as-of output format."""
    strategy = RegimeAwareMomentumStrategy()
    output = []
    for end in range(1, len(history) + 1):
        if end < strategy.config.minimum_history_bars:
            output.append("NO_SIGNAL")
            continue
        causal_bars = tuple(
            Bar(symbol="BTCUSDT", timestamp=b.timestamp, close=b.close)
            for b in history[:end]
        )
        output.append("BUY" if strategy.signal(causal_bars).eligible else "HOLD")
    return tuple(output)


def _symbol_closes(start: str, slope: str) -> list[str]:
    return [
        str(Decimal(start) + Decimal(i) * Decimal(slope))
        for i in range(20)
    ]


def _selection_data():
    return [
        *symbol_bars("BTCUSDT", _symbol_closes("100", "1")),
        *symbol_bars("ETHUSDT", _symbol_closes("110", "0.25")),
    ]


def test_actual_regime_signal_is_causally_stable_on_fixed_daily_sample():
    result = inspect_lookahead(
        _history(), _actual_regime_signals,
        warmup_bars=RegimeAwareMomentumStrategy().config.minimum_history_bars - 1,
    )
    assert result["verdict"] == "NO_HORIZON_DIFFERENCES_IN_THIS_SAMPLE"
    assert result["mismatch_count"] == 0
    assert result["no_lookahead_proven"] is False
    assert result["strategy_promotion_allowed"] is False
    assert result["live_trading_allowed"] is False


def test_real_cross_sectional_selector_is_order_independent_on_aligned_data():
    bars = _selection_data()
    selector = CrossSectionalSelector(top_k=2)
    direct = selector.select(bars)
    reverse = selector.select(list(reversed(bars)))
    assert direct == reverse
    assert len(direct.candidates) == 2
    assert direct.decision_time == bars[-1].timestamp
    assert {row.symbol for row in direct.candidates} == {"BTCUSDT", "ETHUSDT"}


def test_shipped_selector_decision_uses_asof_histories_without_future_day():
    bars = _selection_data()
    selector = CrossSectionalSelector(top_k=2)
    timestamps = sorted({bar.timestamp for bar in bars})
    cutoff = timestamps[-2]
    earlier = [bar for bar in bars if bar.timestamp <= cutoff]
    expected = selector.select(earlier)
    # Mutate a future OHLCV event, then prove the *as-of* decision unchanged.
    revised = []
    for bar in bars:
        if bar.symbol == "BTCUSDT" and bar.timestamp == timestamps[-1]:
            revised.append(replace(
                bar, open=Decimal("1"), high=Decimal("1"),
                low=Decimal("1"), close=Decimal("1"), vwap=Decimal("1"),
            ))
        else:
            revised.append(bar)
    assert selector.select([bar for bar in revised if bar.timestamp <= cutoff]) == expected
    assert expected.decision_time == cutoff
    assert expected.decision_time < timestamps[-1]


def test_selector_rejects_mismatched_latest_symbol_timestamps():
    bars = _selection_data()
    filtered = [bar for bar in bars if not (
        bar.symbol == "ETHUSDT" and bar.timestamp == max(
            row.timestamp for row in bars if row.symbol == "ETHUSDT"
        )
    )]
    with pytest.raises(ValueError, match="same decision timestamp"):
        CrossSectionalSelector(top_k=2).select(filtered)


def test_regime_adapter_is_not_admitted_as_broker_or_profitability_proof():
    audit = inspect_lookahead(_history(), _actual_regime_signals, warmup_bars=7)
    assert audit["evaluation_class"] == "OFFLINE_RESEARCH_SIGNAL_HORIZON_SENSITIVITY_ONLY"
    assert audit["signal_implementation_identity_verified"] is False
    assert audit["historical_profitability_proven"] is False
