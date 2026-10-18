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
            "symbol": "BTCUSDT", "entry_time": "2026-01-02T00:00:00+00:00",
            "exit_time": "2026-01-02T00:00:00+00:00",
            "quantity": "1", "entry_reference_price": "100",
            "exit_reference_price": "120", "entry_execution_price": "100",
            "exit_execution_price": "120", "entry_fee": "1",
            "exit_fee": "1", "gross_pnl_before_costs": "20",
            "slippage_cost": "0", "net_pnl": "18",
            "holding_bars": 0, "exit_reason": "INTRABAR_HARD_STOP",
            "ambiguous_intrabar_exit": False,
        }],
        "final_open_positions": [],
        "funding_events": [{
            "symbol": "BTCUSDT", "entry_time": "2026-01-02T00:00:00+00:00",
            "settlement_time": "2026-01-02T08:00:00+00:00",
            "booked_after_day_utc": "2026-01-03T00:00:00+00:00",
            "activity_id": "research-funding:BTCUSDT:2026-01-02T08:00:00+00:00",
            "quantity": "1", "entry_execution_price": "100",
            "provider_rate": "0.01", "cost_if_held_usd": "1",
            "cash_amount_usd": "-1", "participation_uncertain": True,
            "holding_state": "INTRABAR_EXIT",
            "mark_price_verified": False,
        }],
        "funding_days": [{
            "day_utc": "2026-01-02T00:00:00+00:00",
            "net_funding_cash_usd": "-1", "nonzero_flow_count": 1,
            "uncertain_event_count": 1,
        }],
        "daily_equity": [{
            "day_utc": "2026-01-02T00:00:00+00:00", "prior_equity": "10000",
            "selected_symbols": ["BTCUSDT"], "entered_symbols": ["BTCUSDT"],
            "closing_equity": "10017",
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

def test_terminal_daily_equity_mismatch_rejected():
    r = _record()
    r["daily_equity"][-1]["closing_equity"] = "10018"
    with pytest.raises(PortfolioMoneyInvariantError, match="terminal daily equity"):
        _audit(r)


def test_funding_before_entry_rejected():
    r = _record()
    r["funding_events"][0]["entry_time"] = "2026-01-03T00:00:00+00:00"
    with pytest.raises(PortfolioMoneyInvariantError, match="settled before entry"):
        _audit(r)


def test_entry_fee_schedule_detects_coordinated_trade_misstatement():
    r = _record()
    with pytest.raises(PortfolioMoneyInvariantError, match="entry slippage bps"):
        audit_portfolio_model(
            r, scenario="UPPER_COST", allowed_symbols=("BTCUSDT",),
            fee_bps_per_fill="8", slippage_bps_per_side="5",
        )

def test_daily_equity_prior_balance_tamper_rejected():
    r = _record()
    r["daily_equity"][0]["prior_equity"] = "10001"
    with pytest.raises(PortfolioMoneyInvariantError, match="carry-forward"):
        _audit(r)


def test_daily_decision_must_contain_entered_symbol():
    r = _record()
    r["daily_equity"][0]["selected_symbols"] = []
    with pytest.raises(PortfolioMoneyInvariantError, match="decision/entry"):
        _audit(r)


def test_missing_funding_day_fails_closed():
    r = _record()
    r["funding_days"] = []
    with pytest.raises(PortfolioMoneyInvariantError, match="unbooked funding"):
        _audit(r)


def test_monetary_coherence_does_not_hide_forged_position_exit():
    r = _record()
    r["trades"][0]["exit_reason"] = "SELECTION_EXIT"
    with pytest.raises(PortfolioMoneyInvariantError, match="position lifecycle"):
        _audit(r)


def test_monetary_coherence_does_not_hide_forged_funding_position():
    r = _record()
    # Monetary sums remain unchanged, but the funding lifecycle is impossible.
    r["funding_events"][0]["holding_state"] = "OPEN_AT_CLOSE"
    with pytest.raises(PortfolioMoneyInvariantError, match="position lifecycle"):
        _audit(r)
