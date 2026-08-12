from decimal import Decimal

from tools.evaluate_crypto_capacity import build_capacity_report


def test_capacity_report_compares_two_x_and_three_x_without_promotion() -> None:
    report = build_capacity_report(
        opening_equity_usdt=Decimal("1000"),
        requested_trades_per_day=100,
    )

    assert report["qualification"] == "CRYPTO_CAPACITY_DIAGNOSTIC"
    assert report["purpose"] == "COST_AND_TURNOVER_BOUND_ONLY_NOT_A_PROFIT_FORECAST"
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_order_writes_allowed"] is False
    assert report["live_promotion_allowed"] is False
    scenarios = report["scenarios"]
    two_x = scenarios["2X_NOTIONAL"]["targets"]["TARGET_15_USD"]
    three_x = scenarios["3X_NOTIONAL"]["targets"]["TARGET_25_USD"]
    assert two_x["estimated_round_trip_cost_usdt"] == 3.2
    assert two_x["maximum_full_cost_round_trips"] == 6
    assert two_x["requested_frequency_within_cost_budget"] is False
    assert three_x["estimated_round_trip_cost_usdt"] == 4.8
    assert three_x["maximum_full_cost_round_trips"] == 4
    assert three_x["live_promotion_allowed"] is False
