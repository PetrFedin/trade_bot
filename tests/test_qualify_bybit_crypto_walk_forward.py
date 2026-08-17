from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from tools.qualify_bybit_crypto_walk_forward import CryptoWalkForwardPolicy, run_crypto_walk_forward


def _acquisition(*, days: int = 4, bars_per_day: int = 60) -> BybitKlineAcquisition:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars: list[BybitKlineBar] = []
    symbols = (("BTCUSDT", 1), ("ETHUSDT", 1), ("SOLUSDT", -1))
    for day in range(days):
        day_start = start + timedelta(days=day)
        for symbol, direction in symbols:
            base = Decimal("100") + Decimal(day * 5)
            for index in range(bars_per_day):
                close = base + Decimal(direction) * Decimal("0.7") * Decimal(index)
                bars.append(
                    BybitKlineBar(
                        symbol=symbol,
                        start_time=day_start + timedelta(minutes=5 * index),
                        open=close - Decimal(direction) * Decimal("0.10"),
                        high=close + Decimal("0.50"),
                        low=close - Decimal("0.50"),
                        close=close,
                        volume=Decimal("10000"),
                        turnover=Decimal("2000000"),
                    )
                )
    return BybitKlineAcquisition(
        bars=tuple(sorted(bars, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 1},
    )


def _test_policy() -> CryptoWalkForwardPolicy:
    return CryptoWalkForwardPolicy(
        fold_days=1,
        minimum_folds=4,
        minimum_total_closed_trades=1,
        minimum_positive_fold_fraction=Decimal("0"),
        minimum_aggregate_profit_factor=Decimal("0.01"),
        maximum_worst_fold_drawdown_pct=Decimal("100"),
        require_zero_risk_budget_breaches=False,
    )


def test_walk_forward_uses_non_overlapping_cold_start_chronological_folds() -> None:
    report = run_crypto_walk_forward(
        _acquisition(),
        opening_equity_usdt=Decimal("1000"),
        policy=_test_policy(),
    )

    assert report["qualification"] == "CRYPTO_CHRONOLOGICAL_WALK_FORWARD_RESEARCH"
    assert report["method"] == "NON_OVERLAPPING_FIXED_PARAMETER_COLD_START_FOLDS"
    assert report["fold_count"] == 4
    assert report["parameter_tuning_between_folds"] is False
    assert report["cross_fold_position_state_carried"] is False
    assert report["cross_fold_signal_history_carried"] is False
    assert [fold["first_date"] for fold in report["folds"]] == [
        "2026-08-01",
        "2026-08-02",
        "2026-08-03",
        "2026-08-04",
    ]
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_observation_allowed"] is False
    assert report["live_promotion_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False


def test_walk_forward_scores_combined_and_baseline_without_auto_selection() -> None:
    report = run_crypto_walk_forward(
        _acquisition(),
        opening_equity_usdt=Decimal("1000"),
        policy=_test_policy(),
    )

    decisions = report["candidate_decisions"]
    assert "CONDITIONAL_1_5X" in decisions
    assert "CONDITIONAL_COMBINED_RISK" in decisions
    for decision in decisions.values():
        assert decision["strategy_promotion_allowed"] is False
        assert decision["demo_observation_allowed"] is False
        assert decision["live_promotion_allowed"] is False
        assert decision["fold_count"] == 4
    comparison = report["combined_vs_baseline"]
    assert comparison is not None
    assert comparison["automatic_strategy_selection_allowed"] is False


def test_walk_forward_requires_enough_complete_calendar_days() -> None:
    with pytest.raises(ValueError, match="requires 4"):
        run_crypto_walk_forward(
            _acquisition(days=3),
            policy=_test_policy(),
        )


def test_walk_forward_policy_rejects_invalid_positive_fold_fraction() -> None:
    with pytest.raises(ValueError):
        CryptoWalkForwardPolicy(
            minimum_positive_fold_fraction=Decimal("1.01")
        ).validate()
