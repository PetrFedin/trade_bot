from decimal import Decimal

from app.strategy.crypto_capacity import estimate_crypto_trade_capacity


def test_two_x_capacity_quantifies_cost_budget_before_alpha() -> None:
    estimate = estimate_crypto_trade_capacity(
        opening_equity_usdt=Decimal("1000"),
        notional_to_equity=Decimal("2"),
        target_net_profit_usd=Decimal("15"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
    )

    assert estimate.notional_usdt == Decimal("2000")
    assert estimate.estimated_round_trip_cost_usdt == Decimal("3.2")
    assert estimate.execution_cost_budget_usdt == Decimal("20.00")
    assert estimate.maximum_full_cost_round_trips == 6
    assert estimate.minimum_gross_profit_usdt == Decimal("18.2")
    assert estimate.minimum_price_move_fraction == Decimal("0.0091")
    assert estimate.live_promotion_allowed is False


def test_hundred_round_trips_per_day_exceeds_two_percent_cost_budget() -> None:
    estimate = estimate_crypto_trade_capacity(
        opening_equity_usdt=Decimal("1000"),
        notional_to_equity=Decimal("2"),
        target_net_profit_usd=Decimal("20"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
        requested_trades_per_day=100,
    )

    assert estimate.maximum_full_cost_round_trips == 6
    assert estimate.requested_frequency_within_cost_budget is False
    assert estimate.theoretical_daily_net_target_usdt == Decimal("2000")


def test_three_x_notional_reduces_cost_budget_round_trip_capacity() -> None:
    estimate = estimate_crypto_trade_capacity(
        opening_equity_usdt=Decimal("1000"),
        notional_to_equity=Decimal("3"),
        target_net_profit_usd=Decimal("25"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
    )

    assert estimate.notional_usdt == Decimal("3000")
    assert estimate.estimated_round_trip_cost_usdt == Decimal("4.8")
    assert estimate.execution_cost_budget_usdt == Decimal("20.00")
    assert estimate.maximum_full_cost_round_trips == 4
    assert estimate.minimum_gross_profit_usdt == Decimal("29.8")
    assert estimate.minimum_price_move_fraction == Decimal("29.8") / Decimal("3000")


def test_requested_frequency_at_capacity_is_reported_as_arithmetic_not_forecast() -> None:
    estimate = estimate_crypto_trade_capacity(
        opening_equity_usdt=Decimal("1000"),
        notional_to_equity=Decimal("2"),
        target_net_profit_usd=Decimal("15"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
        requested_trades_per_day=6,
    )

    assert estimate.requested_frequency_within_cost_budget is True
    assert estimate.theoretical_daily_net_target_usdt == Decimal("90")
    assert estimate.live_promotion_allowed is False
