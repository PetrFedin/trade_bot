"""Adversarial research-only lifecycle tests, independent of strategy engine."""
import copy

import pytest

from tools.verify_funded_position_lifecycle import (
    PortfolioLifecycleError,
    audit_position_lifecycle,
)


def _case():
    first = "2026-01-01T00:00:00+00:00"
    second = "2026-01-02T00:00:00+00:00"
    return {
        "allowed_symbols": ("BTCUSDT", "ETHUSDT"),
        "trades": [{
            "symbol": "BTCUSDT", "entry_time": first, "exit_time": second,
            "quantity": "1", "entry_execution_price": "100",
            "holding_bars": 1, "exit_reason": "SELECTION_EXIT",
            "ambiguous_intrabar_exit": False,
        }],
        "final_open_positions": [{
            "symbol": "ETHUSDT", "entry_time": second,
            "quantity": "2", "entry_execution_price": "50",
        }],
        "daily_equity": [
            {"day_utc": first, "entered_symbols": ["BTCUSDT"]},
            {"day_utc": second, "entered_symbols": ["ETHUSDT"]},
        ],
        "funding_events": [
            {"symbol": "BTCUSDT", "settlement_time": first, "entry_time": first,
             "quantity": "1", "entry_execution_price": "100",
             "holding_state": "OPEN_AT_CLOSE", "participation_uncertain": True},
            {"symbol": "BTCUSDT", "settlement_time": second, "entry_time": first,
             "quantity": "1", "entry_execution_price": "100",
             "holding_state": "OPEN_EXIT", "participation_uncertain": True},
            {"symbol": "ETHUSDT", "settlement_time": "2026-01-02T08:00:00+00:00",
             "entry_time": second, "quantity": "2", "entry_execution_price": "50",
             "holding_state": "OPEN_AT_CLOSE", "participation_uncertain": False},
        ],
    }


def _check(record):
    return audit_position_lifecycle(**record)


def test_closed_then_open_reconstructed_from_decisions():
    result = _check(_case())
    assert result["closed_trade_intervals"] == 1
    assert result["terminal_open_intervals"] == 1
    assert result["funding_events_bound_to_positions"] == 3
    assert result["broker_execution_verified"] is False


@pytest.mark.parametrize("mutate,expected", [
    (lambda r: r["daily_equity"][0].update(entered_symbols=[]), "daily entries"),
    (lambda r: r["funding_events"][0].update(quantity="2"), "notional"),
    (lambda r: r["funding_events"][1].update(holding_state="OPEN_AT_CLOSE"), "holding state"),
    (lambda r: r["funding_events"][2].update(participation_uncertain=True),
     "uncertainty"),
    (lambda r: r["funding_events"][1].update(
        settlement_time="2026-01-02T08:00:00+00:00"), "open exit"),
    (lambda r: r["trades"][0].update(holding_bars=2), "holding duration"),
    (lambda r: r["trades"][0].update(exit_reason="UNQUALIFIED"),
     "exit classification"),
    (lambda r: r["final_open_positions"][0].update(
        exit_time="2026-01-02T00:00:00+00:00"), "schema conflated"),
    (lambda r: r["funding_events"][0].update(symbol="FAKE"),
     "without an exposed instrument"),
])
def test_lifecycle_inconsistency_rejected(mutate, expected):
    record = _case()
    mutate(record)
    with pytest.raises(PortfolioLifecycleError, match=expected):
        _check(record)


def test_duplicate_intraday_position_intervals_rejected():
    record = _case()
    other = copy.deepcopy(record["trades"][0])
    other["entry_time"] = "2026-01-02T00:00:00+00:00"
    other["exit_time"] = "2026-01-02T00:00:00+00:00"
    other["exit_reason"] = "INTRABAR_HARD_STOP"
    other["holding_bars"] = 0
    record["trades"].append(other)
    record["daily_equity"][1]["entered_symbols"].append("BTCUSDT")
    with pytest.raises(PortfolioLifecycleError, match="overlapping"):
        _check(record)


def test_same_day_intrabar_closed_trade_is_valid():
    record = _case()
    record["trades"][0]["exit_time"] = "2026-01-01T00:00:00+00:00"
    record["trades"][0]["holding_bars"] = 0
    record["trades"][0]["exit_reason"] = "INTRABAR_HARD_STOP"
    record["funding_events"] = [{
        "symbol": "BTCUSDT", "entry_time": "2026-01-01T00:00:00+00:00",
        "settlement_time": "2026-01-01T08:00:00+00:00",
        "quantity": "1", "entry_execution_price": "100",
        "holding_state": "INTRABAR_EXIT", "participation_uncertain": True,
    }]
    assert _check(record)["funding_events_bound_to_positions"] == 1


def test_missing_day_rejected():
    record = _case()
    record["daily_equity"].pop(0)
    with pytest.raises(PortfolioLifecycleError, match="interval outside"):
        _check(record)
