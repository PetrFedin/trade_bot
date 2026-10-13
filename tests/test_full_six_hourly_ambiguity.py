"""All original 17 six-major daily collisions have full real 60m source coverage."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tools.full_six_hourly_ambiguity import reconstruct_all_six_major

BASE = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
PRIOR = Path("research/snapshots/bybit_intraday_trade_cases_60m")
ADDITIONAL = Path("research/snapshots/bybit_ambiguous_six_hourly_20261013")


def test_all_seventeen_preselected_cases_covered_without_unobserved_fills() -> None:
    payload = reconstruct_all_six_major(
        bars_dir=BASE,
        original_hourly_dir=PRIOR,
        additional_hourly_dir=ADDITIONAL,
    )
    summary = payload["summary"]
    assert summary["preselected_cases"] == 17
    assert summary["pinned_prior_hourly_cases"] == 4
    assert summary["new_provider_hourly_cases"] == 13
    assert summary["total_hourly_candles"] == 408
    assert summary["outcome_unavailable_cases"] == 0
    assert sum(summary["hourly_order_counts"].values()) == 17
    assert len(payload["source_sha256"]) == 17
    assert abs(
        Decimal(summary["original_daily_conservative_trade_net_usd"])
        - Decimal("-357.5666065671900662867824510")
    ) < Decimal("0.000001")
    assert payload["research_only"] is True
    assert payload["strategy_changed"] is False
    assert payload["strategy_promotion_allowed"] is False
    assert payload["live_trading_allowed"] is False
    assert payload["untouched_holdout"] is False
    assert payload["intraday_full_portfolio_backtest_run"] is False
    assert payload["execution_venue_fills_verified"] is False
    assert payload["funding_allocated_per_case"] is False
    assert len(payload["evidence_sha256"]) == 64
    for case in payload["cases"]:
        assert case["public_bybit_hourly_ohlcv_verified_against_daily"] is True
        assert case["actual_order_execution_verified"] is False
        assert case["portfolio_recomputed_after_conditional_exit"] is False
        assert case["source_hourly_candles"] == 24
        assert len(case["source_sha256"]) == 64
        assert case["hourly_first_touch_order"]["ordering"] in {
            "TAKE_PROFIT_FIRST_HOURLY",
            "PROTECTIVE_FIRST_HOURLY",
            "UNRESOLVED_SAME_HOUR",
        }


def test_full_census_is_bitwise_repeatable_on_same_sources() -> None:
    first = reconstruct_all_six_major(
        bars_dir=BASE, original_hourly_dir=PRIOR,
        additional_hourly_dir=ADDITIONAL,
    )
    second = reconstruct_all_six_major(
        bars_dir=BASE, original_hourly_dir=PRIOR,
        additional_hourly_dir=ADDITIONAL,
    )
    assert first == second
    assert first["evidence_sha256"] == second["evidence_sha256"]


def test_absent_real_source_must_block_analysis(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing preselected real 1h source"):
        reconstruct_all_six_major(
            bars_dir=BASE,
            original_hourly_dir=PRIOR,
            additional_hourly_dir=tmp_path,
        )
