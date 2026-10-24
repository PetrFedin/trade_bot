"""Fail-closed historical portfolio risk attribution and market-regime tests."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tools.independent_coin_strategy_lab import Candle
from tools.shared_capital_candidate_study import study_shared
from tools.shared_portfolio_risk_attribution import (
    RiskAuditError,
    _model_risk,
    build_report,
    prior_close_regime,
    verify_sources,
)

BARS = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
BTC = BARS / "BTCUSDT_D.csv"


@pytest.fixture(scope="module")
def original():
    report = study_shared(BARS)
    return report, verify_sources(report, BTC)


def test_real_36_portfolios_regime_and_risk_reconcile(original):
    shared, btc = original
    report = build_report(shared, btc)
    assert report["scenario_count"] == 36
    assert report["historical_days"] == 930
    assert report["cost_comparison_count"] == 18
    assert sum(report["BTC_regime_days"].values()) == 930
    assert report["profitability_proven"] is False
    assert report["unseen_holdout"] is False
    assert report["live_trading_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["source_shared_sha256"] == shared["report_sha256"]
    assert len(report["report_sha256"]) == 64
    for model in report["models"]:
        assert sum(
            row["days"] for row in model["BTC_previous_close_regimes"].values()
        ) == 930
        assert model["daily_cash_audited"] is True
        assert model["regime_cash_audited"] is True
        assert model["worst_5pct_day_count"] == 47
        assert model["longest_daily_close_underwater_days"] <= 930
        assert model["trades"]["closed_trade_count"] >= 2


def test_market_classification_never_reads_future_candle():
    beginning = datetime(2024, 1, 1, tzinfo=UTC)
    series = [
        Candle(
            beginning + timedelta(days=i), D("100") + D(i),
            D("102") + D(i), D("98") + D(i),
            D("101") + D(i), D("1000"),
        )
        for i in range(90)
    ]
    value = prior_close_regime(series, 64)
    changed = list(series)
    changed[64:] = [
        replace(bar, close=bar.close * 20, high=bar.high * 20)
        for bar in series[64:]
    ]
    assert value == prior_close_regime(changed, 64)
    with pytest.raises(RiskAuditError):
        prior_close_regime(series, 49)


def test_modified_original_digest_fails_before_reporting(original):
    shared, _ = original
    modified = copy.deepcopy(shared)
    modified["scenarios"][0]["ending_equity_usdt"] = "999999"
    with pytest.raises(RiskAuditError, match="hash"):
        verify_sources(modified, BTC)


def test_bad_cash_ledger_does_not_pass_even_if_regime_is_valid(original):
    shared, btc = original
    modified = copy.deepcopy(shared["scenarios"][0])
    modified["daily_equity"][0]["free_cash"] = "12345"
    day = [bar.day.isoformat() for bar in btc]
    clock = {
        day[i]: prior_close_regime(btc, i)
        for i in range(51, len(day))
    }
    with pytest.raises(RiskAuditError, match="cash and asset"):
        _model_risk(modified, clock)


def test_forged_trade_net_cannot_pass_cash_and_fees_audit(original):
    shared, btc = original
    modified = copy.deepcopy(shared["scenarios"][0])
    modified["closed_trades"][0]["net_usdt"] = "100000"
    day = [bar.day.isoformat() for bar in btc]
    clock = {
        day[i]: prior_close_regime(btc, i)
        for i in range(51, len(day))
    }
    with pytest.raises(RiskAuditError, match="gross minus"):
        _model_risk(modified, clock)


def test_modified_fee_stress_pair_does_not_hide_losing_scenarios(original):
    shared, btc = original
    report = build_report(shared, btc)
    assert len(report["models"]) == 36
    assert any(D(m["return_pct"]) < 0 for m in report["models"])
    assert any(D(p["high_minus_low_percentage_points"]) < 0
               for p in report["cost_pairs"])
    assert all(p["universe"] in ("SIX", "BROAD21") for p in report["cost_pairs"])


def test_snapshot_source_sha_alone_cannot_inflate_confidence(original):
    shared, btc = original
    report = build_report(shared, btc)
    for key in (
        "account_fee_tier_verified", "historical_mark_price_verified",
        "real_funding_settlement_verified", "historical_order_book_verified",
    ):
        assert report[key] is False
