"""Synthetic journal fixtures are never exchange fills or untouched holdouts."""
from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.historical_execution_journal import (
    JournalError,
    analyze_model,
    build_journal,
)

START = datetime(2024, 1, 1, tzinfo=UTC)


def stamp(day: int) -> str:
    return (START + timedelta(days=day)).isoformat()


def fixture() -> dict:
    daily = []
    for i in range(95):
        daily.append({
            "day_utc": stamp(i),
            "prior_equity": "10000",
            "closing_equity": "10010" if i == 94 else "10000",
            "selected_symbols": ["BTCUSDT"],
            "entered_symbols": ["BTCUSDT"] if i == 0 else [],
        })
    trade = {
        "symbol": "BTCUSDT", "entry_time": stamp(0), "exit_time": stamp(94),
        "entry_reference_price": "100", "exit_reference_price": "101",
        "entry_execution_price": "100", "exit_execution_price": "101",
        "entry_fee": "0.5", "exit_fee": "0.5", "slippage_cost": "0",
        "gross_pnl_before_costs": "11", "quantity": "11", "net_pnl": "10",
        "holding_bars": 94, "exit_reason": "TIME_STOP",
        "ambiguous_intrabar_exit": False, "gap_through_stop": False,
    }
    comparison = {
        "period_id": "six_matched_window",
        "symbols": ["BTCUSDT"], "first_execution_day_utc": stamp(0),
        "last_source_day_utc": stamp(94),
        "fee_bps_per_fill": "8", "slippage_bps_per_side": "5",
        "models": {},
    }
    for model in ("DAILY_STOP_FIRST", "HOURLY_COLLISION_REFINED"):
        for funding in ("NO_FUNDING", "UPPER_COST", "LOWER_COST"):
            comparison["models"][f"{model}:{funding}"] = {
                "metrics": {
                    "ending_equity_usd": "10010",
                    "return_including_daily_funding": "0.001",
                    "closed_trades": 1, "fills": 2,
                    "closed_trade_net_excluding_funding_usd": "10",
                    "execution_fees_usd": "1",
                    "daily_close_max_drawdown_over_peak": "0",
                    "funding_cash_net_usd": "0",
                },
                "daily_equity": copy.deepcopy(daily),
                "trades": [copy.deepcopy(trade)],
                "final_open_positions": [],
                "funding_events": [],
                "funding_days": [] if funding == "NO_FUNDING" else [
                    {"day_utc": stamp(i), "net_funding_cash_usd": "0"}
                    for i in range(95)
                ],
            }
    return comparison


def report() -> dict:
    comparisons = []
    for period in ("six_full_history", "six_matched_window", "broad21_matched_window"):
        for fee, slip in (("8", "5"), ("10", "10")):
            c = fixture()
            c["period_id"] = period
            c["fee_bps_per_fill"] = fee
            c["slippage_bps_per_side"] = slip
            comparisons.append(c)
    payload = {
        "schema_version": "astra-daily-funding-cash-feedback-v1",
        "evaluation_class": "REUSED_HISTORY_DAILY_CASH_FEEDBACK_NOT_VENUE_SETTLEMENT",
        "research_only": True,
        "strategy_promotion_allowed": False, "live_trading_allowed": False,
        "untouched_holdout": False, "funding_exact_exchange_timing": False,
        "mark_price_used": False, "margin_or_liquidation_modelled": False,
        "comparisons": comparisons,
    }
    payload["evidence_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def synthetic_auditor_receipt(payload: dict) -> dict:
    """Only unit fixtures; production CLI requires the actual separate auditor."""
    return {
        "arithmetic_verification": "PASS",
        "configurations": 6,
        "model_replays": 36,
        "closed_trades_checked": 36,
        "funding_events_checked": 0,
        "daily_equity_rows_checked": 36 * 95,
        "matched_dates_verified": True,
        "verification_method": "second implementation from raw rate CSV, trades and daily prices",
        "independent_human_or_agent_review": False,
        "execution_or_venue_account_truth_verified": False,
        "live_trading_allowed": False,
        "evidence_sha256": payload["evidence_sha256"],
    }


def resign(payload: dict) -> None:
    payload["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes({k: v for k, v in payload.items() if k != "evidence_sha256"})
    ).hexdigest()


def test_model_journal_90_day_equity_is_not_closed_trade_pnl():
    c = fixture()
    r = analyze_model(c, "HOURLY_COLLISION_REFINED:UPPER_COST")
    assert r["closed_trades"] == 1
    assert r["holding_days"]["longer"] == 1
    assert len(r["windows"]) == 2
    assert r["windows"][0]["complete_window"] is True
    assert r["windows"][0]["equity_return_pct"] == "0"
    assert r["windows"][1]["complete_window"] is False
    assert r["windows"][1]["closed_trade_net_pnl_usd_attributed"] == "10"
    assert r["windows"][1]["equity_return_pct"] == "0.100"


def test_all_36_models_and_matched_pairs_no_permission():
    source = report()
    result = build_journal(source, synthetic_auditor_receipt(source))
    assert result["model_count"] == 36
    assert result["scenario_count"] == 6
    assert len(result["matched_universe_differences"]) == 12
    assert result["live_trading_allowed"] is False
    assert result["profitability_proven"] is False
    assert result["actual_exchange_fills_verified"] is False


@pytest.mark.parametrize("tamper", [
    "clock", "carry", "pnl", "fee", "trade", "funding", "entry",
])
def test_false_historical_trade_or_account_linkage_fails_closed(tamper):
    c = fixture()
    m = c["models"]["HOURLY_COLLISION_REFINED:UPPER_COST"]
    if tamper == "clock":
        m["daily_equity"][1]["day_utc"] = stamp(0)
    elif tamper == "carry":
        m["daily_equity"][1]["prior_equity"] = "10001"
    elif tamper == "pnl":
        m["trades"][0]["net_pnl"] = "12"
    elif tamper == "fee":
        m["trades"][0]["entry_fee"] = "0.1"
    elif tamper == "trade":
        m["trades"][0]["holding_bars"] = 93
    elif tamper == "funding":
        m["funding_days"][0]["net_funding_cash_usd"] = "5"
    else:
        m["daily_equity"][0]["entered_symbols"] = ["ETHUSDT"]
    with pytest.raises(JournalError):
        analyze_model(c, "HOURLY_COLLISION_REFINED:UPPER_COST")


def test_rehashed_unqualified_live_claim_is_rejected():
    p = report()
    p["live_trading_allowed"] = True
    resign(p)
    with pytest.raises(JournalError, match="scope"):
        build_journal(p, synthetic_auditor_receipt(p))


def test_rehashed_duplicate_scenario_is_rejected():
    p = report()
    p["comparisons"][0] = copy.deepcopy(p["comparisons"][1])
    resign(p)
    with pytest.raises(JournalError, match="matrix"):
        build_journal(p, synthetic_auditor_receipt(p))


def test_source_report_byte_mutation_without_rehash_rejected():
    p = report()
    p["comparisons"][0]["models"]["DAILY_STOP_FIRST:UPPER_COST"]["metrics"][
        "ending_equity_usd"
    ] = "0"
    with pytest.raises(JournalError, match="hash"):
        build_journal(p, synthetic_auditor_receipt(p))


def test_rehashed_report_cannot_replace_independently_audited_source():
    source = report()
    frozen_receipt = synthetic_auditor_receipt(source)
    source["comparisons"][0]["models"]["DAILY_STOP_FIRST:UPPER_COST"][
        "metrics"
    ]["ending_equity_usd"] = "10011"
    resign(source)
    with pytest.raises(JournalError, match="evidence_sha256"):
        build_journal(source, frozen_receipt)


def test_mismatched_independent_auditor_population_rejected():
    source = report()
    receipt = synthetic_auditor_receipt(source)
    receipt["daily_equity_rows_checked"] -= 1
    with pytest.raises(JournalError, match="population mismatch"):
        build_journal(source, receipt)


def test_false_independent_venue_account_claim_rejected():
    source = report()
    receipt = synthetic_auditor_receipt(source)
    receipt["execution_or_venue_account_truth_verified"] = True
    with pytest.raises(JournalError, match="unsafe claim"):
        build_journal(source, receipt)


def test_missing_independent_verifier_rejected():
    source = report()
    with pytest.raises(JournalError, match="missing independent auditor"):
        build_journal(source, None)


def test_closed_trade_without_matching_decision_entry_rejected():
    c = fixture()
    m = c["models"]["HOURLY_COLLISION_REFINED:UPPER_COST"]
    m["daily_equity"][0]["entered_symbols"] = []
    with pytest.raises(JournalError, match="no recorded"):
        analyze_model(c, "HOURLY_COLLISION_REFINED:UPPER_COST")


def test_phantom_selection_entry_without_trade_or_terminal_holding_rejected():
    c = fixture()
    m = c["models"]["HOURLY_COLLISION_REFINED:UPPER_COST"]
    m["daily_equity"][1]["entered_symbols"] = ["BTCUSDT"]
    with pytest.raises(JournalError, match="do not conserve"):
        analyze_model(c, "HOURLY_COLLISION_REFINED:UPPER_COST")


def test_terminal_holding_cannot_reuse_closed_trade_entry_identity():
    c = fixture()
    m = c["models"]["HOURLY_COLLISION_REFINED:UPPER_COST"]
    m["final_open_positions"] = [{
        "symbol": "BTCUSDT", "entry_time": stamp(0), "quantity": "1",
    }]
    with pytest.raises(JournalError, match="final holding lacks unique"):
        analyze_model(c, "HOURLY_COLLISION_REFINED:UPPER_COST")
