from datetime import UTC, datetime
from decimal import Decimal

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.ohlcv_exit import (
    IntrabarExitReason,
    IntrabarPositionState,
    evaluate_long_intrabar_exit,
)
from app.strategy.position_management import PositionManagementPolicy

TIME = datetime(2026, 1, 2, tzinfo=UTC)
POLICY = PositionManagementPolicy()


def bar(*, open: str, high: str, low: str, close: str) -> OhlcvBar:
    return OhlcvBar(
        symbol="AAPL",
        timestamp=TIME,
        open=Decimal(open),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=1000,
        trade_count=100,
        vwap=Decimal(close),
    )


def state(peak: str = "100") -> IntrabarPositionState:
    return IntrabarPositionState(peak_completed_price=Decimal(peak))


def test_hard_stop_uses_intrabar_low() -> None:
    result = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open="100", high="101", low="97", close="99"),
        state=state(),
        policy=POLICY,
    )
    assert result.exit_now is True
    assert result.reason is IntrabarExitReason.HARD_STOP
    assert result.exit_price_before_costs == Decimal("98.00")
    assert result.ambiguous_bar is False


def test_take_profit_uses_intrabar_high() -> None:
    result = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open="100", high="105", low="99", close="103"),
        state=state(),
        policy=POLICY,
    )
    assert result.exit_now is True
    assert result.reason is IntrabarExitReason.TAKE_PROFIT
    assert result.exit_price_before_costs == Decimal("104.00")


def test_same_bar_stop_and_take_chooses_conservative_protective_exit() -> None:
    result = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open="100", high="105", low="97", close="102"),
        state=state(),
        policy=POLICY,
    )
    assert result.exit_now is True
    assert result.reason is IntrabarExitReason.HARD_STOP
    assert result.exit_price_before_costs == Decimal("98.00")
    assert result.ambiguous_bar is True


def test_gap_below_stop_uses_worse_open_price() -> None:
    result = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open="95", high="97", low="94", close="96"),
        state=state(),
        policy=POLICY,
    )
    assert result.reason is IntrabarExitReason.HARD_STOP
    assert result.exit_price_before_costs == Decimal("95")
    assert result.gap_through_protective_stop is True


def test_prior_completed_peak_activates_trailing_stop() -> None:
    result = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open="104", high="106", low="103", close="104"),
        state=state("105"),
        policy=POLICY,
    )
    assert result.reason is IntrabarExitReason.TRAILING_STOP
    assert result.trailing_stop_price == Decimal("103.425")
    assert result.exit_price_before_costs == Decimal("103.425")


def test_current_bar_high_cannot_retroactively_arm_same_bar_trailing_stop() -> None:
    first = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open="100", high="103", low="101", close="102.5"),
        state=state(),
        policy=POLICY,
    )
    assert first.exit_now is False
    assert first.trailing_stop_price is None
    assert first.state.peak_completed_price == Decimal("103")

    second = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open="102", high="102.5", low="101", close="101.5"),
        state=first.state,
        policy=POLICY,
    )
    assert second.exit_now is True
    assert second.reason is IntrabarExitReason.TRAILING_STOP
    assert second.trailing_stop_price == Decimal("101.455")


def test_gap_above_take_profit_keeps_conservative_target_price() -> None:
    result = evaluate_long_intrabar_exit(
        average_cost=Decimal("100"),
        bar=bar(open="106", high="107", low="105", close="106"),
        state=state(),
        policy=POLICY,
    )
    assert result.reason is IntrabarExitReason.TAKE_PROFIT
    assert result.exit_price_before_costs == Decimal("104.00")
