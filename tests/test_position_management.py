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
    result = decide("101.50", peak="103")
    assert result.exit_now
    assert result.reason is ExitReason.TRAILING_STOP
    assert result.peak_reference_price == Decimal("103")


def test_time_stop_prevents_indefinite_position_holding() -> None:
    result = decide("101", index=10)
    assert result.exit_now
    assert result.reason is ExitReason.TIME_STOP
