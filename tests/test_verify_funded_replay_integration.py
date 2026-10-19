"""Fail-closed gates for full historical monetary integration."""
import hashlib
import json

import pytest

from tools.verify_funded_replay_integration import (
    HistoricalMoneyIntegrationError,
    audit_real_historical_money,
)


def _fixture():
    report = {
        "schema_version": "astra-daily-funding-cash-feedback-v1",
        "evaluation_class": "REUSED_HISTORY_DAILY_CASH_FEEDBACK_NOT_VENUE_SETTLEMENT",
        "research_only": True, "funding_cash_feedback_modelled": True,
        "mark_price_used": False, "notional_model": "CONSTANT_ENTRY_EXECUTION_NOTIONAL",
        "untouched_holdout": False,
        "cash_booking_clock": "AFTER_DAILY_TRADES_BEFORE_NEXT_DAY_SIZING",
        "comparisons": [],
    }
    for flag in (
        "strategy_promotion_allowed", "live_trading_allowed",
        "funding_exact_exchange_timing",
        "funding_same_hour_exit_refinement_used", "intraday_reinvestment",
        "margin_or_liquidation_modelled",
    ):
        report[flag] = False
    for period in ("six_full_history", "six_matched_window", "broad21_matched_window"):
        for fee, slip in (("8", "5"), ("10", "10")):
            report["comparisons"].append({
                "period_id": period, "fee_bps_per_fill": fee,
                "slippage_bps_per_side": slip,
                "symbols": [
                    "BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT",
                    "XRPUSDT",
                ] if period != "broad21_matched_window" else [
                    "BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT",
                    "XRPUSDT",
                ] + [f"T{i}" for i in range(15)],
                "first_source_day_utc": "2023-10-09T00:00:00+00:00",
                "first_execution_day_utc": "2023-10-09T00:00:00+00:00",
                "last_source_day_utc": "2026-06-14T00:00:00+00:00",
                "models": {
                    f"{model}:{scenario}": {}
                    for model in ("DAILY_STOP_FIRST", "HOURLY_COLLISION_REFINED")
                    for scenario in ("NO_FUNDING", "UPPER_COST", "LOWER_COST")
                },
            })
    unsigned = json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    report["evidence_sha256"] = hashlib.sha256(unsigned).hexdigest()
    verifier = {
        "arithmetic_verification": "PASS",
        "evidence_sha256": report["evidence_sha256"],
        "configurations": 6, "model_replays": 36,
        "matched_dates_verified": True,
        "independent_human_or_agent_review": False,
        "execution_or_venue_account_truth_verified": False,
        "live_trading_allowed": False,
    }
    return report, verifier


def _check(report, verifier):
    return audit_real_historical_money(
        report, verifier,
        report_bytes=b"report", verifier_bytes=b"receipt",
        historical_head_sha="6f6c3c42ef3b33a1fbeb2a45131b014ee15810f8",
    )


def test_complete_evidence_grid_with_isolated_money_stub(monkeypatch):
    import tools.verify_funded_replay_integration as module
    monkeypatch.setattr(module, "audit_portfolio_model", lambda *a, **kw: {})
    result = _check(*_fixture())
    assert result["model_count"] == 36
    assert result["profitability_proven"] is False


def test_missing_model_never_passes(monkeypatch):
    import tools.verify_funded_replay_integration as module
    monkeypatch.setattr(module, "audit_portfolio_model", lambda *a, **kw: {})
    report, verifier = _fixture()
    del report["comparisons"][0]["models"]["DAILY_STOP_FIRST:LOWER_COST"]
    with pytest.raises(HistoricalMoneyIntegrationError):
        _check(report, verifier)


def test_promotion_flag_never_passes_even_with_fresh_hash():
    report, verifier = _fixture()
    report["strategy_promotion_allowed"] = True
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in report.items() if k != "evidence_sha256"},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    verifier["evidence_sha256"] = report["evidence_sha256"]
    with pytest.raises(HistoricalMoneyIntegrationError, match="unsafe report"):
        _check(report, verifier)


def test_unverified_verifier_must_not_pass():
    report, verifier = _fixture()
    verifier["arithmetic_verification"] = "FAIL"
    with pytest.raises(HistoricalMoneyIntegrationError, match="independent verifier"):
        _check(report, verifier)
