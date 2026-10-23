"""Safe model checks for a long-only shared-cash research ledger."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tools.independent_coin_strategy_lab import BROAD21, Candle
from tools.shared_capital_candidate_study import (
    BENCHMARK,
    CANDIDATES,
    RANKERS,
    SharedCapitalError,
    replay_shared,
    study_shared,
)

SNAP = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")


def tiny(length=105):
    first = datetime(2024, 1, 1, tzinfo=UTC)
    result = {}
    for symbol, slope in (("BTCUSDT", 3), ("ETHUSDT", 2), ("SOLUSDT", 4)):
        result[symbol] = []
        for i in range(length):
            close = D(100 + slope * i)
            result[symbol].append(Candle(
                first + timedelta(days=i), close - 1, close, close - 2,
                close, D(1000 + 10 * i),
            ))
    return result


def test_cash_and_two_slots_with_fixed_benchmark():
    r = replay_shared(tiny(), BENCHMARK)
    assert r["closed_trade_count"] == 2
    assert set(t["symbol"] for t in r["closed_trades"]) == {
        "BTCUSDT", "ETHUSDT",
    }
    assert all(len(x["open_symbols"]) <= 2 for x in r["daily_equity"])
    assert all(D(x["free_cash"]) >= 0 for x in r["daily_equity"])
    assert D(r["max_entry_exposure_fraction"]) <= D("0.60")
    assert abs(
        D(r["ending_equity_usdt"]) - D("10000") -
        D(r["closed_trade_net_usdt"])
    ) < D("0.000001")
    assert r["live_trading_allowed"] is False


@pytest.mark.parametrize("arm", CANDIDATES)
@pytest.mark.parametrize("ranker", RANKERS)
def test_prefix_cannot_change_past_decisions(arm, ranker):
    complete = tiny(125)
    prefix = {symbol: bars[:90] for symbol, bars in complete.items()}
    a = replay_shared(complete, arm, ranker=ranker, finalize=False)
    b = replay_shared(prefix, arm, ranker=ranker, finalize=False)
    n = len(b["decision_trace"])
    assert b["decision_trace"] == a["decision_trace"][:n]
    assert b["daily_equity"] == a["daily_equity"][:n]


def test_costs_debit_same_cash_book():
    lo = replay_shared(tiny(), BENCHMARK, fee_bps=D("8"), slippage_bps=D("5"))
    hi = replay_shared(tiny(), BENCHMARK, fee_bps=D("10"), slippage_bps=D("10"))
    assert D(hi["ending_equity_usdt"]) < D(lo["ending_equity_usdt"])


def test_synchronization_and_invalid_configuration_fail_closed():
    book = tiny()
    with pytest.raises(SharedCapitalError):
        replay_shared(book, "UNKNOWN")
    with pytest.raises(SharedCapitalError):
        replay_shared(book, BENCHMARK, ranker="PREVIOUS_20D_RETURN")
    book["BTCUSDT"] = book["BTCUSDT"][1:]
    with pytest.raises(SharedCapitalError):
        replay_shared(book, BENCHMARK)


def test_real_common_capital_36_models_and_cash_reconciliation():
    report = study_shared(SNAP)
    assert report["scenario_count"] == 36
    assert report["shared_days"] == 981
    assert report["symbols"] == list(BROAD21)
    assert report["strategy_promotion_allowed"] is False
    assert report["profitability_proven"] is False
    assert len(report["source_sha256"]) == 21
    assert len(report["report_sha256"]) == 64
    for r in report["scenarios"]:
        assert D(r["max_entry_exposure_fraction"]) <= D("0.60")
        assert all(len(x["open_symbols"]) <= 2 for x in r["daily_equity"])
        assert all(D(x["free_cash"]) >= 0 for x in r["daily_equity"])
        assert abs(
            D(r["ending_equity_usdt"]) - D("10000") -
            D(r["closed_trade_net_usdt"])
        ) < D("0.000001") * max(1, r["closed_trade_count"])
    for fee, slip in (("8", "5"), ("10", "10")):
        benchmark = [
            r for r in report["scenarios"]
            if r["arm"] == BENCHMARK and
            (r["fee_bps_per_fill"], r["slippage_bps_per_fill"]) == (fee, slip)
        ]
        assert len(benchmark) == 2
        assert benchmark[0]["daily_equity"] == benchmark[1]["daily_equity"]
