"""Causal, cash-constrained BTC gate experiments: no market order authority."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tools.causal_btc_entry_gate_study import BASELINE_SHA, GATES, build_gate_report
from tools.independent_coin_strategy_lab import Candle
from tools.shared_capital_candidate_study import (
    CANDIDATES,
    RANKERS,
    SharedCapitalError,
    _reject_btc_entry,
    replay_shared,
)

ARCHIVE = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")


def synthetic(length: int = 116, alternating: bool = False):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    universe = {}
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        rows = []
        btc = D("100")
        for i in range(length):
            if sym == "BTCUSDT":
                btc *= D("1.1") if alternating and i % 2 else (
                    D("0.9") if alternating else D("0.998")
                )
                close = btc
            else:
                close = D("100") + i * (D("3") if sym == "ETHUSDT" else D("2"))
            rows.append(Candle(
                start + timedelta(days=i),
                close,
                close + D("1"),
                close - D("1"),
                close,
                D("1000") + i * D("20"),
            ))
        universe[sym] = rows
    return universe


@pytest.mark.parametrize("gate", GATES)
def test_btc_research_gate_never_reads_today_or_future(gate):
    first = synthetic(alternating=gate == "BTC_VOLATILITY_100_BLOCK")
    i = 63
    before = _reject_btc_entry(first, i, gate)
    changed = {
        sym: [*bars[:i], *(
            replace(bar, close=bar.close * D("10"),
                    high=bar.high * D("10"))
            for bar in bars[i:]
        )]
        for sym, bars in first.items()
    }
    assert _reject_btc_entry(changed, i, gate) is before


def test_sma_gate_denies_new_signals_and_never_uses_fake_future_cash():
    universe = synthetic()
    no_gate = replay_shared(universe, "BREAKOUT", finalize=False)
    gated = replay_shared(
        universe, "BREAKOUT", entry_gate="BTC_SMA50_BLOCK", finalize=False,
    )
    assert len(no_gate["decision_trace"][0]["entered"]) > 0
    assert gated["decision_trace"][0]["entered"] == []
    assert gated["entry_block_counts"]["BTC_PRIOR_CLOSE_ENTRY_GATE"] >= 1
    assert gated["live_trading_allowed"] is False
    assert all(D(x["free_cash"]) >= 0 for x in gated["daily_equity"])


def test_volatility_gate_responds_only_to_prior_close_variance():
    universe = synthetic(alternating=True)
    assert _reject_btc_entry(
        universe, 63, "BTC_VOLATILITY_100_BLOCK"
    ) is True


@pytest.mark.parametrize("arm", CANDIDATES)
@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("ranker", RANKERS)
def test_prior_close_gates_do_not_revise_past_decisions(arm, gate, ranker):
    complete = synthetic()
    prefix = {symbol: bars[:88] for symbol, bars in complete.items()}
    long = replay_shared(
        complete, arm, ranker=ranker, entry_gate=gate, finalize=False,
    )
    short = replay_shared(
        prefix, arm, ranker=ranker, entry_gate=gate, finalize=False,
    )
    n = len(short["decision_trace"])
    assert short["decision_trace"] == long["decision_trace"][:n]
    assert short["daily_equity"] == long["daily_equity"][:n]


def test_unregistered_gate_and_benchmark_gating_rejected():
    universe = synthetic()
    with pytest.raises(SharedCapitalError):
        replay_shared(universe, "BREAKOUT", entry_gate="BLACK_BOX_ALPHA")
    with pytest.raises(SharedCapitalError):
        replay_shared(universe, "BUY_HOLD_BTC_ETH", entry_gate=GATES[0])


@pytest.fixture(scope="module")
def genuine_gate_study():
    return build_gate_report(ARCHIVE)


def test_frozen_original_and_complete_64_genuine_variants(genuine_gate_study):
    report = genuine_gate_study
    assert report["scenario_count"] == 64
    assert report["baseline_shared_report_sha256"] == BASELINE_SHA
    assert report["historical_days"] == 930
    assert len(report["source_sha256"]) == 21
    assert report["profitability_proven"] is False
    assert report["untouched_future_holdout"] is False
    assert report["live_trading_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["funding_cash_settlement_verified"] is False
    assert len(report["report_sha256"]) == 64
    assert any(D(x["model"]["return_pct"]) < 0 for x in report["scenarios"])
    assert all(x["model"]["entry_gate_policy"] in GATES
               for x in report["scenarios"])
    for case in report["scenarios"]:
        model = case["model"]
        assert D(model["max_entry_exposure_fraction"]) <= D("0.60")
        assert all(len(m["open_symbols"]) <= 2 and D(m["free_cash"]) >= 0
                   for m in model["daily_equity"])
        assert abs(
            D(model["ending_equity_usdt"]) - D("10000")
            - D(model["closed_trade_net_usdt"])
        ) < D("0.000001") * max(1, model["closed_trade_count"])
