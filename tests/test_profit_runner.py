from datetime import UTC, datetime
from decimal import Decimal

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.ohlcv_exit import (
    IntrabarExitReason,
    IntrabarPositionState,
    evaluate_long_intrabar_exit,
)
from app.strategy.position_management import (
    PositionManagementPolicy,
    PositionTrackingState,
    TakeProfitMode,
    evaluate_position_exit,
)

NOW = datetime(2026, 8, 12, tzinfo=UTC)


def bar(*, open_: str, high: str, low: str, close: str) -> OhlcvBar:
    return OhlcvBar(
        symbol="AAPL",
        timestamp=NOW,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1_000_000,
        trade_count=10_000,
        vwap=Decimal(close),
    )


def runner_policy() -> PositionManagementPolicy:
    return PositionManagementPolicy(
        stop_loss_fraction=Decimal("0.02"),
        take_profit_fraction=Decimal("0.04"),
        trailing_activation_fraction=Decimal("0.02"),
        trailing_stop_fraction=Decimal("0.015"),
        maximum_holding_bars=10,
        break_even_activation_fraction=Decimal("0.01"),
        break_even_buffer_fraction=Decimal("0.001"),
        profit_protection_activation_fraction=Decimal("0.015"),
        maximum_profit_giveback_fraction=Decimal("0.50"),
        take_profit_mode=TakeProfitMode.PROFIT_RUNNER,
    )


def test_fixed_take_profit_retains_legacy_exit() -> None:
    decision = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open_="100", high="105", low="99", close="104.5"),
        state=IntrabarPositionState(peak_completed_price=Decimal("100")),
        policy=PositionManagementPolicy(),
    )

    assert decision.exit_now is True
    assert decision.reason is IntrabarExitReason.TAKE_PROFIT
    assert decision.exit_price_before_costs == Decimal("104")


def test_profit_runner_does_not_retroactively_arm_same_bar_protection() -> None:
    decision = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open_="100", high="105", low="99", close="104.5"),
        state=IntrabarPositionState(peak_completed_price=Decimal("100")),
        policy=runner_policy(),
    )

    assert decision.exit_now is False
    assert decision.reason is None
    assert decision.state.peak_completed_price == Decimal("105")
    assert decision.trailing_stop_price is None
    assert decision.profit_protection_stop_price is None


def test_profit_runner_locks_gain_from_next_bar_completed_peak() -> None:
    first = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open_="100", high="105", low="99", close="104.5"),
        state=IntrabarPositionState(peak_completed_price=Decimal("100")),
        policy=runner_policy(),
    )
    second = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open_="104.8", high="105", low="103", close="103.4"),
        state=first.state,
        policy=runner_policy(),
    )

    assert second.exit_now is True
    assert second.reason is IntrabarExitReason.TRAILING_STOP
    assert second.exit_price_before_costs == Decimal("103.425")
    assert second.exit_price_before_costs > Decimal("100")


def test_close_based_profit_runner_does_not_cap_winner_at_take_profit() -> None:
    decision = evaluate_position_exit(
        average_cost=Decimal("100"),
        reference_price=Decimal("105"),
        state=PositionTrackingState(
            entry_execution_index=0,
            peak_reference_price=Decimal("104"),
        ),
        current_execution_index=3,
        policy=runner_policy(),
    )

    assert decision.exit_now is False
    assert decision.profit_fraction == Decimal("0.05")
    assert decision.protected_stop_price > Decimal("100")
