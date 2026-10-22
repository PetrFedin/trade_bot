"""Fixed historical candidate research tests; no OOS or exchange-fill claims."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tools.independent_coin_strategy_lab import (
    ARMS,
    BROAD21,
    COSTS,
    Candle,
    StrategyStudyError,
    _exit_reference,
    digest,
    read_candles,
    run_strategy,
    signal,
    study,
)

BASE = datetime(2024, 1, 1, tzinfo=UTC)
BARS_DIR = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")


def bars(n=150):
    return [
        Candle(
            BASE + timedelta(days=i), Decimal(100 + i),
            Decimal(102 + i), Decimal(98 + i), Decimal(101 + i),
            Decimal(100000 + i * 100),
        )
        for i in range(n)
    ]


def test_candidate_signal_uses_no_future_completed_candle():
    originals = bars()
    index = 85
    changed = originals[:index] + [
        replace(b, high=Decimal("100000"), close=Decimal("99999"))
        for b in originals[index:]
    ]
    for arm in ARMS:
        assert signal(originals, index, arm) == signal(changed, index, arm)
        assert signal(originals[:index], index, arm) == signal(
            originals, index, arm
        )


def test_stop_target_collision_and_gap_stop_first():
    candle = bars()[61]
    position = {"entry_reference": Decimal("100"), "entry_index": 61}
    both = replace(
        candle, open=Decimal("101"), high=Decimal("107"),
        low=Decimal("95"), close=Decimal("102"),
    )
    assert _exit_reference(position, both, 61, "BREAKOUT") == (
        Decimal("97"), "STOP_FIRST"
    )
    gap = replace(both, open=Decimal("91"), low=Decimal("90"))
    assert _exit_reference(position, gap, 61, "BREAKOUT") == (
        Decimal("91"), "GAP_STOP"
    )


def test_model_equity_reconciles_and_funding_is_not_imagined():
    result = run_strategy(
        bars(), "BUY_HOLD_29PC",
        fee_bps=Decimal("8"), slip_bps=Decimal("5"),
    )
    assert result["trades"] == 1
    assert result["trade_tape"][0]["reason"] == "ARCHIVE_END_CLOSE"
    assert abs(
        Decimal(result["ending_equity_usdt"]) - Decimal("10000")
        - Decimal(result["closed_trade_net_usdt"])
    ) < Decimal("0.000001")
    assert Decimal(result["fees_paid_usdt"]) > 0
    assert result["funding_accounted"] is False
    assert result["live_trading_allowed"] is False


def test_prefix_does_not_rewrite_past_trade_or_equity():
    sequence = bars(160)
    for arm in ARMS:
        full = run_strategy(
            sequence, arm, fee_bps=Decimal("8"),
            slip_bps=Decimal("5"), finalize=False,
        )
        short = run_strategy(
            sequence[:110], arm, fee_bps=Decimal("8"),
            slip_bps=Decimal("5"), finalize=False,
        )
        n = len(short["decision_tape"])
        assert short["decision_tape"] == full["decision_tape"][:n]
        assert short["equity_curve"] == full["equity_curve"][:n]
        assert short["trade_tape"] == [
            row for row in full["trade_tape"]
            if row["exit_utc"] <= sequence[109].day.isoformat()
        ]


def test_unknown_policy_and_extreme_fee_fail_closed():
    with pytest.raises(StrategyStudyError, match="unknown"):
        run_strategy(
            bars(), "GUARANTEED_ALPHA", fee_bps=Decimal("8"),
            slip_bps=Decimal("5"),
        )
    with pytest.raises(StrategyStudyError, match="cost"):
        run_strategy(
            bars(), "IMPULSE", fee_bps=Decimal("200"),
            slip_bps=Decimal("5"),
        )


def test_invalid_historical_duplicate_csv_is_not_accepted(tmp_path):
    path = tmp_path / "BTCUSDT_D.csv"
    path.write_text(
        "timestamp,symbol,open,high,low,close,volume\n"
        "2024-01-01T00:00:00+00:00,BTCUSDT,100,102,98,101,1000\n"
        "2024-01-01T00:00:00+00:00,BTCUSDT,100,102,98,101,1000\n",
        encoding="utf-8",
    )
    with pytest.raises(StrategyStudyError, match="unsorted"):
        read_candles(path, "BTCUSDT")


def test_genuine_21_coin_matched_history_all_candidates_have_costs():
    report = study(BARS_DIR, BROAD21)
    assert report["scenario_count"] == 210
    assert report["synchronized_days"] == 981
    assert len(report["candidate_arms"]) == 5
    assert len(report["universe_descriptive_summary"]) == 20
    assert report["historical_orderbook_verified"] is False
    assert report["historical_funding_settlement_verified"] is False
    assert report["multi_coin_portfolio_result"] is False
    assert report["profitability_proven"] is False
    assert report["live_trading_allowed"] is False
    assert len(report["source_sha256"]) == 21
    assert len(report["report_sha256"]) == 64
    assert any(row["return_pct"].startswith("-") for row in report["scenarios"])


def test_fee_profiles_and_strategy_arms_are_frozen():
    assert tuple(str(x) for x in COSTS[0]) == ("8", "5")
    assert tuple(str(x) for x in COSTS[1]) == ("10", "10")
    assert len(ARMS) == len(set(ARMS))
    assert len(digest({"a": 1})) == 64
