from decimal import Decimal

from app.strategy.position_management import (
    ExitReason,
    PositionManagementPolicy,
    PositionTrackingState,
    evaluate_position_exit,
)


def decide(price: str, *, peak: str = "100", index: int = 1, entry: int = 0):
    return evaluate_position_exit(
        average_cost=Decimal("100"),
        reference_price=Decimal(price),
        state=PositionTrackingState(entry, Decimal(peak)),
        current_execution_index=index,
        policy=PositionManagementPolicy(),
    )


def test_stop_loss_exits_before_loss_can_expand_without_bound() -> None:
    result = decide("97")
    assert result.exit_now
    assert result.reason is ExitReason.STOP_LOSS
    assert result.profit_fraction == Decimal("-0.03")


def test_take_profit_harvests_closed_gain() -> None:
    result = decide("104")
    assert result.exit_now
    assert result.reason is ExitReason.TAKE_PROFIT
    assert result.profit_fraction == Decimal("0.04")


def test_trailing_stop_requires_prior_profit_activation() -> None:
    result = decide("101.40", peak="103")
    assert result.exit_now
    assert result.reason is ExitReason.TRAILING_STOP
    assert result.peak_reference_price == Decimal("103")
    assert result.drawdown_from_peak_fraction >= Decimal("0.015")


def test_time_stop_prevents_indefinite_position_holding() -> None:
    result = decide("101", index=10)
    assert result.exit_now
    assert result.reason is ExitReason.TIME_STOP


def test_break_even_stop_can_lock_small_gain_after_confirmation() -> None:
    policy = PositionManagementPolicy(
        trailing_activation_fraction=Decimal("0.03"),
        break_even_activation_fraction=Decimal("0.01"),
        break_even_buffer_fraction=Decimal("0.001"),
    )
    result = evaluate_position_exit(
        average_cost=Decimal("100"),
        reference_price=Decimal("100.05"),
        state=PositionTrackingState(0, Decimal("102")),
        current_execution_index=2,
        policy=policy,
    )
    assert result.exit_now
    assert result.reason is ExitReason.BREAK_EVEN_STOP
    assert result.protected_stop_price == Decimal("100.100")


def test_profit_protection_limits_giveback_of_confirmed_mfe() -> None:
    policy = PositionManagementPolicy(
        trailing_activation_fraction=Decimal("0.03"),
        profit_protection_activation_fraction=Decimal("0.02"),
        maximum_profit_giveback_fraction=Decimal("0.50"),
    )
    result = evaluate_position_exit(
        average_cost=Decimal("100"),
        reference_price=Decimal("101.20"),
        state=PositionTrackingState(0, Decimal("102.50")),
        current_execution_index=3,
        policy=policy,
    )
    assert result.exit_now
    assert result.reason is ExitReason.PROFIT_PROTECTION
    assert result.maximum_favorable_excursion_fraction == Decimal("0.025")
    assert result.protected_stop_price == Decimal("101.25000")
