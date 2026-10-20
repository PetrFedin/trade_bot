"""Negative source-continuity and selector-linkage fixtures."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.strategy.cross_sectional_selection import CrossSectionalSelector
from tests.test_cross_sectional_selection import symbol_bars
from tools.inspect_cross_sectional_alignment import (
    ResearchAlignmentError,
    inspect_alignment,
    shadow_select_if_aligned,
)


def _aligned():
    numbers = [str(Decimal("100") + Decimal(i)) for i in range(15)]
    slow = [str(Decimal("120") + Decimal(i) / 4) for i in range(15)]
    return [*symbol_bars("BTCUSDT", numbers), *symbol_bars("ETHUSDT", slow)]


def test_verified_aligned_asof_window_reaches_actual_shadow_selector():
    bars = _aligned()
    report, selection = shadow_select_if_aligned(
        bars, expected_interval=timedelta(days=1),
    )
    assert report["result"] == "SHADOW_LOOKBACK_TIMES_ALIGNED"
    assert report["lookback_bars"] == 8
    assert report["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert report["checked_rows"] == 30
    assert len(report["lookback_timestamps_sha256"]) == 64
    assert report["decision_time"] == selection.decision_time.isoformat()
    assert report["source_origin_verified"] is False
    assert report["full_history_continuity_verified"] is False
    assert report["profitability_proven"] is False
    assert report["live_trading_allowed"] is False


def test_missing_interior_symbol_candle_is_rejected_before_selector():
    bars = _aligned()
    offending = next(row for row in bars if row.symbol == "ETHUSDT")
    cut_timestamp = offending.timestamp + timedelta(days=10)
    gap = [
        bar for bar in bars
        if not (bar.symbol == "ETHUSDT" and bar.timestamp == cut_timestamp)
    ]
    with pytest.raises(ResearchAlignmentError, match="not synchronized"):
        shadow_select_if_aligned(gap)


def test_simultaneous_missing_bar_rejected_only_with_declared_daily_cadence():
    bars = _aligned()
    missing_day = min(bar.timestamp for bar in bars) + timedelta(days=12)
    gaps = [bar for bar in bars if bar.timestamp != missing_day]
    report = inspect_alignment(gaps, indicator_lookback=8)
    assert report["full_history_continuity_verified"] is False
    with pytest.raises(ResearchAlignmentError, match="source cadence gap"):
        inspect_alignment(
            gaps, indicator_lookback=8,
            expected_interval=timedelta(days=1),
        )


def test_duplicate_instrument_timestamp_rejected():
    bars = _aligned()
    with pytest.raises(ResearchAlignmentError, match="duplicate"):
        inspect_alignment(bars + [bars[0]], indicator_lookback=8)


def test_latest_time_misalignment_rejected():
    bars = _aligned()
    last = max(row.timestamp for row in bars)
    missing = [
        row for row in bars
        if not (row.symbol == "ETHUSDT" and row.timestamp == last)
    ]
    with pytest.raises(ResearchAlignmentError, match="not synchronized"):
        shadow_select_if_aligned(missing)


def test_extra_symbols_without_needed_history_rejected():
    bars = _aligned()
    bars += symbol_bars("SOLUSDT", ["100"] * 3)
    with pytest.raises(ResearchAlignmentError, match="insufficient indicator history"):
        shadow_select_if_aligned(bars)


def test_canonical_order_does_not_change_selection():
    bars = _aligned()
    first_gate, first_decision = shadow_select_if_aligned(bars)
    second_gate, second_decision = shadow_select_if_aligned(reversed(bars))
    assert first_gate == second_gate
    assert first_decision == second_decision


def test_bad_price_cannot_reach_selector():
    bars = _aligned()
    bars[0] = replace(bars[0], close=Decimal("NaN"))
    with pytest.raises(ValueError, match="positive and finite"):
        shadow_select_if_aligned(bars)


def test_invalid_selector_type_rejected_without_broker_authority():
    with pytest.raises(ResearchAlignmentError, match="unsupported shadow selector"):
        shadow_select_if_aligned(_aligned(), selector=object())


def test_invalid_lookback_is_never_silently_clipped():
    with pytest.raises(ResearchAlignmentError, match="invalid indicator lookback"):
        inspect_alignment(_aligned(), indicator_lookback=0)


def test_policy_parameters_are_not_modified_by_preflight():
    selector = CrossSectionalSelector(top_k=2)
    selected = shadow_select_if_aligned(_aligned(), selector=selector)[1]
    assert selector.top_k == 2
    assert selector.signal_config.minimum_history_bars == 8
    assert selected.decision_time is not None
