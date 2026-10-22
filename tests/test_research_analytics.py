"""No fabricated research metrics: exact identity, chronology and arithmetic tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.research_analytics import (
    CashScenarioFacts,
    ResearchAnalyticsError,
    calculate_cash_scenarios,
    calculate_research_overview,
)

ROOT = Path(__file__).resolve().parents[1]
STATUS = json.loads((ROOT / "CURRENT_SYSTEM_STATUS.json").read_text())
REGISTER = json.loads(
    (ROOT / "research/competitive_capability_register_v1.json").read_text()
)


def _overview(status=None, register=None):
    return calculate_research_overview(
        STATUS if status is None else status,
        REGISTER if register is None else register,
        evidence_root=ROOT,
    )


def test_frozen_negative_pnl_is_reproduced_with_exact_decimal_math():
    result = _overview()
    assert result["reference_equity_usdt"] == "1000.00"
    assert result["net_pnl_usdt"] == "-176.67"
    assert result["net_return_pct"] == "-17.67"
    assert result["net_pnl_per_trade_usdt"] == "-1.73"
    assert result["win_rate_pct"] == "35.29"
    assert result["loss_rate_pct"] == "53.92"
    assert result["neutral_rate_pct"] == "10.78"
    assert result["plan_eligibility_pct"] == "33.08"
    assert result["counts"]["portfolio_trades"] == 102
    assert (
        result["counts"]["wins"] + result["counts"]["breakeven"]
        + result["counts"]["losses"] == 102
    )
    assert result["capability_summary"]["feature_count"] == 12
    assert result["authority"] == "HOLD"
    assert result["profitability_proven"] is False
    assert result["trading_authority_allowed"] is False
    assert result["max_drawdown_pct"] is None
    assert result["sharpe_ratio"] is None
    assert result["fill_level_cost_stress"] is None
    assert result["actual_exchange_funding_usdt"] is None


def test_research_overview_reproduces_identically():
    assert _overview() == _overview()


@pytest.mark.parametrize("bad", [
    True, -1, "102", 1.1, None,
])
def test_typed_trade_counts_fail_closed(bad):
    modified = copy.deepcopy(STATUS)
    modified["strategy"]["latest_frozen_bybit_price_only_replay"]["portfolio_trades"] = bad
    with pytest.raises(ResearchAnalyticsError, match="invalid historical replay count"):
        _overview(modified)


def test_disagreeing_episode_components_refused():
    modified = copy.deepcopy(STATUS)
    modified["strategy"]["latest_frozen_bybit_price_only_replay"]["target_first"] += 1
    with pytest.raises(ResearchAnalyticsError, match="contradictory"):
        _overview(modified)


def test_disagreeing_trade_components_refused():
    modified = copy.deepcopy(STATUS)
    modified["strategy"]["latest_frozen_bybit_price_only_replay"]["wins"] += 1
    with pytest.raises(ResearchAnalyticsError, match="contradictory"):
        _overview(modified)


def test_approximate_return_cannot_contradict_pnl():
    modified = copy.deepcopy(STATUS)
    replay = modified["strategy"]["latest_frozen_bybit_price_only_replay"]
    replay["net_return_fraction_approx"] = "0.20"
    with pytest.raises(ResearchAnalyticsError, match="disagree"):
        _overview(modified)


@pytest.mark.parametrize("key", [
    "external_order_routing_allowed", "demo_trading_allowed",
    "live_trading_allowed", "mainnet_entry_allowed", "production_release_allowed",
])
def test_research_console_refuses_unsupported_trading_authority(key):
    modified = copy.deepcopy(STATUS)
    modified["trading_authority"][key] = True
    with pytest.raises(ResearchAnalyticsError, match="research-only"):
        _overview(modified)


def test_new_research_capability_must_not_claim_live_authority():
    modified = copy.deepcopy(REGISTER)
    modified["capabilities"][0]["trading_authority_allowed"] = True
    with pytest.raises(ValueError, match="cannot grant"):
        _overview(register=modified)


def test_cash_scenario_multiplicative_turnover_bps_and_signed_funding():
    facts = CashScenarioFacts(
        gross_pnl_usdt="1000.00",
        turnover_notional_usdt="20000",
        fees_paid_usdt="12.00",
        slippage_paid_usdt="8",
        funding_debit_usdt="5",
    )
    result = calculate_cash_scenarios(facts)
    assert result["base_modeled_net_usdt"] == "975.00"
    assert result["rows"] == [
        {"additional_adverse_bps": "0", "additional_cost_usdt": "0.00",
         "modeled_net_usdt": "975.00"},
        {"additional_adverse_bps": "5", "additional_cost_usdt": "10.00",
         "modeled_net_usdt": "965.00"},
        {"additional_adverse_bps": "10", "additional_cost_usdt": "20.00",
         "modeled_net_usdt": "955.00"},
        {"additional_adverse_bps": "25", "additional_cost_usdt": "50.00",
         "modeled_net_usdt": "925.00"},
    ]
    assert result["observed_execution_proven"] is False
    assert result["trading_authority_allowed"] is False


def test_funding_credit_increases_research_net_without_fake_execution():
    result = calculate_cash_scenarios(CashScenarioFacts(
        gross_pnl_usdt="0", turnover_notional_usdt="1000",
        fees_paid_usdt="2", slippage_paid_usdt="1", funding_debit_usdt="-5",
    ))
    assert result["base_modeled_net_usdt"] == "2.00"
    assert result["observed_execution_proven"] is False


@pytest.mark.parametrize("overrides", [
    {"turnover_notional_usdt": "0"},
    {"turnover_notional_usdt": "-1"},
    {"fees_paid_usdt": "-0.01"},
    {"slippage_paid_usdt": "-1"},
    {"gross_pnl_usdt": "NaN"},
    {"fees_paid_usdt": "Infinity"},
    {"funding_debit_usdt": "1e9999"},
    {"gross_pnl_usdt": 10},
])
def test_unsupported_money_inputs_fail_closed(overrides):
    base = {
        "gross_pnl_usdt": "100",
        "turnover_notional_usdt": "2000",
        "fees_paid_usdt": "10",
        "slippage_paid_usdt": "5",
        "funding_debit_usdt": "2",
    }
    base.update(overrides)
    with pytest.raises(ResearchAnalyticsError):
        calculate_cash_scenarios(CashScenarioFacts(**base))


def test_half_even_precision_only_rounds_after_full_basis_point_multiplication():
    above_tie = calculate_cash_scenarios(CashScenarioFacts(
        gross_pnl_usdt="0", turnover_notional_usdt="10.00000001",
        fees_paid_usdt="0", slippage_paid_usdt="0", funding_debit_usdt="0",
    ))
    exact_tie = calculate_cash_scenarios(CashScenarioFacts(
        gross_pnl_usdt="0", turnover_notional_usdt="10",
        fees_paid_usdt="0", slippage_paid_usdt="0", funding_debit_usdt="0",
    ))
    assert above_tie["rows"][1]["additional_cost_usdt"] == "0.01"
    assert above_tie["rows"][1]["modeled_net_usdt"] == "-0.01"
    assert exact_tie["rows"][1]["additional_cost_usdt"] == "0.00"
    assert exact_tie["rows"][1]["modeled_net_usdt"] == "0.00"
