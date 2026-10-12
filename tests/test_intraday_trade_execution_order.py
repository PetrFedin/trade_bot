"""Real-source hourly reconstruction is observational and cannot promote strategy."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tools.intraday_trade_execution_order import _trigger_order, inspect_cases

BASE = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
CASES = Path("research/snapshots/bybit_intraday_trade_cases_60m")


def test_same_hour_stop_and_target_remains_unresolved() -> None:
    report = _trigger_order(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": "100",
                "high": "106",
                "low": "95",
                "close": "101",
            }
        ],
        protective=Decimal("98"),
        take_profit=Decimal("104"),
    )
    assert report["ordering"] == "UNRESOLVED_SAME_HOUR"
    assert report["first_protective_hour"] == 0
    assert report["first_target_hour"] == 0
    assert report["remaining_same_hour_path_uncertainty"] is True


def test_protective_first_on_separate_hours_cannot_be_relabelled_as_target() -> None:
    report = _trigger_order(
        [
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "open": "100", "high": "101", "low": "97", "close": "98",
            },
            {
                "timestamp": "2024-01-01T01:00:00Z",
                "open": "98", "high": "105", "low": "97", "close": "104",
            },
        ],
        protective=Decimal("98"),
        take_profit=Decimal("104"),
    )
    assert report["ordering"] == "PROTECTIVE_FIRST_HOURLY"


def test_real_frozen_bybit_examples_reconcile_without_promotion() -> None:
    report = inspect_cases(bars_dir=BASE, cases_dir=CASES)
    assert report["strategy_promotion_allowed"] is False
    assert report["live_trading_allowed"] is False
    assert report["untouched_holdout"] is False
    assert report["provider_authenticity_independently_verified"] is False
    assert report["original_closed_trade_count"] == 256
    assert len(report["cases"]) == 4

    cases = {(case["symbol"], case["exit_day"]): case for case in report["cases"]}
    assert cases["ETHUSDT", "2024-02-28"]["hourly"]["ordering"] == (
        "TAKE_PROFIT_FIRST_HOURLY"
    )
    assert cases["BNBUSDT", "2024-04-12"]["hourly"]["ordering"] == (
        "TAKE_PROFIT_FIRST_HOURLY"
    )
    assert cases["ETHUSDT", "2024-03-04"]["hourly"]["ordering"] == (
        "PROTECTIVE_FIRST_HOURLY"
    )
    for case in report["cases"]:
        assert case["daily_ambiguous"] is True
        assert case["conditional_net_not_portfolio_replay"] is True
        assert (
            Decimal(case["ledger_average_cost_including_entry_fee"])
            > Decimal(case["entry_execution_price"])
        )
        assert len(case["source_sha256"]) == 64
    assert len(report["evidence_sha256"]) == 64


def test_same_pinned_historical_cases_produce_same_evidence_sha() -> None:
    first = inspect_cases(bars_dir=BASE, cases_dir=CASES)
    second = inspect_cases(bars_dir=BASE, cases_dir=CASES)
    assert first["evidence_sha256"] == second["evidence_sha256"]
    assert first == second
