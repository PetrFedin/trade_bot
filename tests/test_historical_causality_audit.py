"""Synthetic proof of fail-closed causal audit behavior; not exchange trades."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.marketdata.ohlcv import OhlcvBar
from tools.daily_funding_cash_replay import replay
from tools.historical_causality_audit import (
    CausalityError,
    _baseline,
    compare_prefix,
    source_hash_gate,
)

D = Decimal
START = datetime(2024, 1, 1, tzinfo=UTC)


def bars():
    out = []
    for symbol, step in (("BTCUSDT", D("1")), ("ETHUSDT", D("0.7"))):
        for i in range(120):
            open_price = D("100") + D(i) * step
            out.append(OhlcvBar(
                symbol, START + timedelta(days=i), open_price,
                open_price + D("1.5"), open_price - D("1.5"), open_price,
                100000, 100,
            ))
    return out


def run_pair():
    rows = bars()
    full = replay(rows, D("8"), D("5"))
    truncated = replay([
        b for b in rows if b.timestamp < START + timedelta(days=75)
    ], D("8"), D("5"))
    return full, truncated


def test_real_portfolio_replay_decisions_invariant_to_future_truncation():
    full, short = run_pair()
    result = compare_prefix(full, short)
    assert result["historical_prefix_invariant"] is True
    assert result["compared_decisions"] == len(short.decision_trace)


def test_changed_past_selection_rejected_even_when_equity_unchanged():
    full, short = run_pair()
    traces = list(short.decision_trace)
    traces[5] = replace(traces[5], selected_symbols=("CHANGEDUSDT",))
    with pytest.raises(CausalityError, match="past decision"):
        compare_prefix(full, replace(short, decision_trace=tuple(traces)))


def test_changed_closed_trade_economics_rejected():
    full, short = run_pair()
    if not short.closed_trades:
        pytest.skip("synthetic market generated no closed trades")
    trades = list(short.closed_trades)
    trades[0] = replace(trades[0], net_pnl=trades[0].net_pnl + D("1"))
    with pytest.raises(CausalityError, match="earlier closed trade"):
        compare_prefix(full, replace(short, closed_trades=tuple(trades)))


def test_mismatched_funding_day_rejected():
    full, short = run_pair()
    end = short.decision_trace[-1].execution_time
    a = SimpleNamespace(
        daily_audit=[{"day_utc": end.isoformat(), "cash": "-1"}], audit=[]
    )
    b = SimpleNamespace(
        daily_audit=[{"day_utc": end.isoformat(), "cash": "0"}], audit=[]
    )
    with pytest.raises(CausalityError, match="funding daily cash"):
        compare_prefix(full, short, full_book=a, prefix_book=b)


def test_mismatched_hourly_first_touch_rejected():
    full, short = run_pair()
    end = short.decision_trace[-1].execution_time
    a = SimpleNamespace(audit=[
        {"day_utc": end.isoformat(), "status": "TARGET_FIRST_HOURLY"}
    ])
    b = SimpleNamespace(audit=[
        {"day_utc": end.isoformat(), "status": "PROTECTIVE_FIRST_HOURLY"}
    ])
    with pytest.raises(CausalityError, match="hourly protective"):
        compare_prefix(full, short, full_hourly=a, prefix_hourly=b)


def test_missing_hourly_source_refuses_positive_causality_claim():
    full, short = run_pair()
    end = short.decision_trace[-1].execution_time
    a = SimpleNamespace(audit=[
        {"day_utc": end.isoformat(), "status": "MISSING_SOURCE_STOP_FIRST"}
    ])
    with pytest.raises(CausalityError, match="missing genuine hourly"):
        compare_prefix(full, short, full_hourly=a, prefix_hourly=a)


def test_mixed_funding_model_rejected():
    full, short = run_pair()
    with pytest.raises(CausalityError, match="funding replay model"):
        compare_prefix(full, short, full_book=SimpleNamespace())


def test_wrong_frozen_comparison_rejected():
    report = {"comparisons": [{
        "period_id": "six_full_history",
        "fee_bps_per_fill": "8",
        "slippage_bps_per_side": "5",
        "models": {},
    }]}
    with pytest.raises(CausalityError, match="missing frozen"):
        _baseline(report, "six_full_history", D("8"), D("5"),
                  "HOURLY_COLLISION_REFINED:UPPER_COST")


def test_symlink_root_source_rejected(tmp_path):
    root = tmp_path / "snapshots"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (root / "bybit_3y_2023-10-09_2026-10-08").symlink_to(
        other, target_is_directory=True
    )
    with pytest.raises(CausalityError, match="source directory must be plain"):
        source_hash_gate(root, {"source_daily_and_funding_sha256": {}})


def test_terminal_equity_discrepancy_rejected():
    full, short = run_pair()
    with pytest.raises(CausalityError, match="terminal equity"):
        compare_prefix(full, replace(short, ending_equity=short.ending_equity + D("1")))
