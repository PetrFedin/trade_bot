"""Independent monetary checks on deterministic, synthetic research ledgers."""
import copy

import pytest

from tools.verify_funded_portfolio_money import (
    PortfolioMoneyInvariantError,
    audit_portfolio_model,
)


def _record():
    return {
        "metrics": {
            "closed_trade_net_excluding_funding_usd": "18",
            "execution_fees_usd": "2",
            "funding_cash_net_usd": "-1",
            "funding_debits_usd": "1",
            "funding_credits_usd": "0",
            "open_cost_usd": "0",
            "open_marked_value_usd": "0",
            "ending_cash_usd": "10017",
            "ending_equity_usd": "10017",
            "return_including_daily_funding": "0.0017",
            "closed_trades": 1, "fills": 2,
            "daily_close_max_drawdown_over_peak": "0",
        },
        "trades": [{
            "symbol": "BTCUSDT", "entry_time": "2026-01-01T00:00:00+00:00",
            "exit_time": "2026-01-02T00:00:00+00:00",
            "quantity": "1", "entry_reference_price": "100",
            "exit_reference_price": "120", "entry_execution_price": "100",
            "exit_execution_price": "120", "entry_fee": "1",
            "exit_fee": "1", "gross_pnl_before_costs": "20",
            "slippage_cost": "0", "net_pnl": "18",
        }],
        "final_open_positions": [],
        "funding_events": [{
            "symbol": "BTCUSDT", "settlement_time": "2026-01-01T08:00:00+00:00",
            "booked_after_day_utc": "2026-01-02T00:00:00+00:00",
            "activity_id": "research-funding:BTCUSDT:2026-01-01T08:00:00+00:00",
            "quantity": "1", "entry_execution_price": "100",
            "provider_rate": "0.01", "cost_if_held_usd": "1",
            "cash_amount_usd": "-1", "participation_uncertain": False,
            "mark_price_verified": False,
        }],
        "funding_days": [{
            "day_utc": "2026-01-01T00:00:00+00:00",
            "net_funding_cash_usd": "-1", "nonzero_flow_count": 1,
            "uncertain_event_count": 0,
        }],
        "daily_equity": [{
            "day_utc": "2026-01-02T00:00:00+00:00", "closing_equity": "10017",
        }],
    }


def _audit(record, scenario="UPPER_COST"):
    return audit_portfolio_model(
        record, scenario=scenario, allowed_symbols=("BTCUSDT",)
    )


def test_balanced_research_ledger():
    assert _audit(_record())["cash_equity_reconciled"] is True


@pytest.mark.parametrize("section,key,value,message", [
    ("metrics", "ending_equity_usd", "10018", "equity reconciliation"),
    ("metrics", "execution_fees_usd", "3", "execution fee sum"),
    ("metrics", "funding_cash_net_usd", "0", "funding cash sum"),
    ("metrics", "funding_debits_usd", "0", "funding debit sum"),
    ("metrics", "return_including_daily_funding", "0.01", "funded portfolio return"),
    ("trades", "net_pnl", "19", "trade net PnL"),
    ("trades", "slippage_cost", "1", "trade slippage"),
    ("funding_events", "cash_amount_usd", "-2", "funding cash"),
    ("funding_events", "provider_rate", "0.02", "funding cost"),
    ("funding_days", "net_funding_cash_usd", "0", "funding day cash"),
])
def test_adversarial_ledger_mutations(section, key, value, message):
    r = copy.deepcopy(_record())
    target = r[section] if section == "metrics" else r[section][0]
    target[key] = value
    with pytest.raises(PortfolioMoneyInvariantError, match=message):
        _audit(r)


def test_false_mark_price_source_claim_rejected():
    r = _record()
    r["funding_events"][0]["mark_price_verified"] = True
    with pytest.raises(PortfolioMoneyInvariantError, match="false exact Mark"):
        _audit(r)


def test_duplicate_funding_event_rejected():
    r = _record()
    r["funding_events"].append(copy.deepcopy(r["funding_events"][0]))
    with pytest.raises(PortfolioMoneyInvariantError, match="duplicate"):
        _audit(r)


def test_nonfinite_cost_rejected():
    r = _record()
    r["trades"][0]["entry_fee"] = "NaN"
    with pytest.raises(PortfolioMoneyInvariantError, match="non-finite"):
        _audit(r)


def test_no_funding_must_have_empty_cashflow():
    with pytest.raises(PortfolioMoneyInvariantError, match="NO_FUNDING"):
        _audit(_record(), scenario="NO_FUNDING")
