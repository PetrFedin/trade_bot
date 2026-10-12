"""Fail-closed real 5m Strategy A exit ordering; no trading authority."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tools.intraday_five_minute_order import (
    _five_minute_order,
    inspect_five_minute_cases,
)

DAILY = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
HOURLY = Path("research/snapshots/bybit_intraday_trade_cases_60m")
FIVE = Path("research/snapshots/bybit_intraday_trade_cases_5m")


def test_5m_same_bar_order_stays_unresolved() -> None:
    sample = [{
        "timestamp": "2024-02-28T15:10:00.000Z",
        "open": "100", "high": "105", "low": "97", "close": "101",
    }]
    verdict = _five_minute_order(
        sample, protective=Decimal("98"), take_profit=Decimal("104")
    )
    assert verdict["ordering"] == "UNRESOLVED_SAME_FIVE_MINUTE_BAR"
    assert verdict["five_minute_path_fully_known"] is False


def test_five_minute_case_evidence_deterministic_and_non_promoting() -> None:
    first = inspect_five_minute_cases(
        bars_dir=DAILY,
        hourly_cases_dir=HOURLY,
        five_minute_cases_dir=FIVE,
    )
    second = inspect_five_minute_cases(
        bars_dir=DAILY,
        hourly_cases_dir=HOURLY,
        five_minute_cases_dir=FIVE,
    )
    assert first == second
    assert first["evidence_sha256"] == second["evidence_sha256"]
    assert first["summary"]["cases"] == 4
    assert first["summary"]["complete_five_minute_candles"] == 1152
    assert first["summary"]["five_minute_target_first"] == 3
    assert first["summary"]["five_minute_protective_first"] == 1
    assert first["summary"]["five_minute_unresolved"] == 0
    assert first["strategy_changed"] is False
    assert first["research_only"] is True
    assert first["untouched_holdout"] is False
    assert first["full_portfolio_pnl_recomputed"] is False
    assert first["strategy_promotion_allowed"] is False
    assert first["live_trading_allowed"] is False
    assert first["provider_authenticity_independently_verified"] is False
    assert len(first["source_sha256"]) == 4

    expected_order = {
        ("ETHUSDT", "2024-02-28"): "TAKE_PROFIT_FIRST_5M",
        ("ETHUSDT", "2024-03-04"): "PROTECTIVE_FIRST_5M",
        ("BNBUSDT", "2024-04-12"): "TAKE_PROFIT_FIRST_5M",
        ("BTCUSDT", "2024-01-30"): "TAKE_PROFIT_FIRST_5M",
    }
    for case in first["cases"]:
        key = case["symbol"], case["exit_day"]
        assert case["five_minute_first_trigger"]["ordering"] == expected_order[key]
        assert case["five_minute_vs_hourly_ohlcv_reconciled"] is True
        assert case["exact_provider_fill_observed"] is False
        assert case["capital_redeployment_replayed"] is False
        assert (
            case["original_daily_net_before_funding"]
            == case["original_daily_net_before_funding"]
        )
