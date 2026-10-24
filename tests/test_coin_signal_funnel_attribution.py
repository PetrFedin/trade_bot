"""Coin-signal opportunity ledger; original source remains immutable and read-only."""
from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path

import pytest

from tools.causal_btc_entry_gate_study import build_gate_report
from tools.coin_signal_funnel_attribution import (
    BASELINE_SHA,
    GATE_SHA,
    RISK_SHA,
    FunnelAuditError,
    analyze_model,
    build_report,
    verify,
)
from tools.shared_capital_candidate_study import study_shared
from tools.shared_portfolio_risk_attribution import (
    build_report as build_risk,
    prior_close_regime,
    verify_sources,
)

ROOT = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
BTC = ROOT / "BTCUSDT_D.csv"


@pytest.fixture(scope="module")
def source():
    baseline = study_shared(ROOT)
    btc = verify_sources(baseline, BTC)
    risk = build_risk(baseline, btc)
    gated = build_gate_report(ROOT)
    return baseline, risk, gated, btc


@pytest.fixture(scope="module")
def funnel(source):
    baseline, risk, gated, _ = source
    return build_report(baseline, gated, risk, BTC)


def test_100_historical_portfolios_and_48_nonoptimized_ranker_pairs(funnel):
    assert funnel["original_shared_sha256"] == BASELINE_SHA
    assert funnel["experimental_gate_sha256"] == GATE_SHA
    assert funnel["risk_regime_sha256"] == RISK_SHA
    assert funnel["original_portfolios"] == 36
    assert funnel["gated_portfolios"] == 64
    assert funnel["total_model_scenarios"] == 100
    assert funnel["ranker_pair_count"] == 48
    assert funnel["matched_daily_marks"] == 930
    assert len(funnel["scenario_records"]) == 100
    assert len(funnel["ranker_pairs"]) == 48
    assert funnel["profitability_proven"] is False
    assert funnel["untouched_future_holdout"] is False
    assert funnel["live_trading_allowed"] is False
    assert funnel["historical_orderbook_verified"] is False
    assert funnel["no_historical_rejected_signal_pnl_assumed"] is True
    assert len(funnel["report_sha256"]) == 64


def test_complete_per_coin_funnel_and_entry_regime_trade_pnl(funnel):
    for model in funnel["scenario_records"]:
        assert (
            model["eligible_signals"]
            == model["accepted_signals"] + model["rejected_signals"]
        )
        assert model["accepted_signals"] == model["closed_trades"]
        assert model["rejected_signal_counterfactual_pnl"] is None
        assert sum(
            c["eligible_signal_days"] for c in model["per_coin"]
        ) == model["eligible_signals"]
        assert sum(
            c["accepted_entries"] for c in model["per_coin"]
        ) == model["accepted_signals"]
        assert sum(
            c["rejected_signal_days"] for c in model["per_coin"]
        ) == model["rejected_signals"]
        for item in model["per_coin"]:
            assert item["rejected_signal_counterfactual_pnl"] is None
            assert (
                item["eligible_signal_days"]
                == item["accepted_entries"] + item["rejected_signal_days"]
            )
            assert item["closed_trades"] == item["accepted_entries"]
            assert sum(
                x["closed_trades_entered_in_regime"]
                for x in item["entry_regimes"].values()
            ) == item["closed_trades"]


def test_actual_blocking_and_inert_high_volatility_gate(funnel):
    original = [
        x for x in funnel["scenario_records"] if x["gate"] == "NONE"
    ]
    sma = [
        x for x in funnel["scenario_records"] if x["gate"] == "BTC_SMA50_BLOCK"
    ]
    vol = [
        x for x in funnel["scenario_records"]
        if x["gate"] == "BTC_VOLATILITY_100_BLOCK"
    ]
    assert len(original) == 36 and len(sma) == len(vol) == 32
    assert any(x["rejection_reasons"].get(
        "BTC_PRIOR_CLOSE_ENTRY_GATE", 0
    ) > 0 for x in sma)
    assert all(x["rejection_reasons"].get(
        "BTC_PRIOR_CLOSE_ENTRY_GATE", 0
    ) == 0 for x in vol)


def test_trade_cash_per_coin_equals_original_portfolio(funnel):
    from decimal import Decimal as D

    for item in funnel["scenario_records"]:
        assert abs(
            sum((D(p["net_usdt"]) for p in item["per_coin"]), D(0))
            - D(item["realized_net_usdt"])
        ) < D("0.000001")
        assert abs(
            sum((D(p["fees_usdt"]) for p in item["per_coin"]), D(0))
            - D(item["realized_fee_usdt"])
        ) < D("0.000001")
        assert abs(
            sum((D(p["slippage_usdt"]) for p in item["per_coin"]), D(0))
            - D(item["realized_slippage_usdt"])
        ) < D("0.000001")


def test_detect_missing_decision_exit_and_per_cohort_money(source):
    baseline, _, _, btc = source
    model = copy.deepcopy(next(
        m for m in baseline["scenarios"]
        if m["universe"] == "BROAD21"
        and m["arm"] == "BREAKOUT"
        and m["entry_ranker"] == "SYMBOL_ASC"
        and m["fee_bps_per_fill"] == "8"
    ))
    days = tuple(b.day.isoformat() for b in btc)[51:]
    clock = {
        btc[i].day.isoformat(): prior_close_regime(btc, i)
        for i in range(51, len(btc))
    }
    model["decision_trace"][0]["ranked_prior_day_signals"].append("BTCUSDT")
    with pytest.raises(FunnelAuditError, match="signal"):
        analyze_model(
            model, label="BROAD21", gate="NONE",
            clock=clock, original_days=days,
        )
    original = copy.deepcopy(next(
        m for m in baseline["scenarios"]
        if m["universe"] == "BROAD21"
        and m["arm"] == "BREAKOUT"
        and m["entry_ranker"] == "SYMBOL_ASC"
        and m["fee_bps_per_fill"] == "8"
    ))
    original["closed_trades"][0]["net_usdt"] = "1000000"
    with pytest.raises(FunnelAuditError, match="closed trade net"):
        analyze_model(
            original, label="BROAD21", gate="NONE",
            clock=clock, original_days=days,
        )


def test_mutated_reports_cannot_sneak_into_original_sha_chain(source):
    original, risk, gated, _ = source
    data = copy.deepcopy(gated)
    data["scenarios"][0]["model"]["ending_equity_usdt"] = "9999999"
    with pytest.raises(FunnelAuditError, match="SHA"):
        build_report(original, data, risk, BTC)
    data = copy.deepcopy(risk)
    data["historical_mark_price_verified"] = True
    with pytest.raises(FunnelAuditError, match="SHA"):
        build_report(original, gated, data, BTC)


def test_wrong_baseline_population_and_digest_rejected(source):
    original, _, _, _ = source
    verify(original, BASELINE_SHA, "shared")
    data = copy.deepcopy(original)
    data["report_sha256"] = "0" * 64
    with pytest.raises(FunnelAuditError, match="SHA"):
        verify(data, BASELINE_SHA, "shared")
    assert sum(Counter(x["gate"] for x in build_report(
        *[source[0], source[2], source[1]], BTC
    )["scenario_records"]).values()) == 100
