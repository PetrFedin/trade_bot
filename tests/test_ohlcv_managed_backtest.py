from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.backtest import BacktestConfig
from app.strategy.ohlcv_managed_backtest import (
    OhlcvExitReason,
    OhlcvManagedHistoricalBacktester,
)
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy

START = datetime(2026, 1, 2, tzinfo=UTC)


def simple_bar(
    index: int,
    close: str,
    *,
    open: str | None = None,
    high: str | None = None,
    low: str | None = None,
) -> OhlcvBar:
    close_value = Decimal(close)
    open_value = close_value if open is None else Decimal(open)
    high_value = max(open_value, close_value) + Decimal("0.1") if high is None else Decimal(high)
    low_value = min(open_value, close_value) - Decimal("0.1") if low is None else Decimal(low)
    return OhlcvBar(
        symbol="AAPL",
        timestamp=START + timedelta(days=index),
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=1000 + index,
        trade_count=100 + index,
        vwap=close_value,
    )


def history() -> list[OhlcvBar]:
    return [simple_bar(index, str(100 + index)) for index in range(8)]


def backtester() -> OhlcvManagedHistoricalBacktester:
    return OhlcvManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
        config=BacktestConfig(
            opening_cash=Decimal("10000"),
            fee_per_fill=Decimal("0"),
            slippage_bps=Decimal("0"),
        ),
    )


def test_previous_signal_enters_next_open_then_intrabar_take_profit_closes_gain() -> None:
    bars = [
        *history(),
        simple_bar(8, "112", open="108", high="113", low="107"),
    ]
    result = backtester().run(bars)
    assert result.fill_count == 2
    assert result.closed_trade_count == 1
    assert result.winning_trades == 1
    assert result.final_quantity == Decimal("0")
    assert result.intrabar_exit_count == 1
    trade = result.closed_trades[0]
    assert trade.entry_execution_price == Decimal("108")
    assert trade.exit_execution_price == Decimal("112.32")
    assert trade.net_pnl == Decimal("4.32")
    assert trade.holding_bars == 0
    assert trade.exit_reason is OhlcvExitReason.INTRABAR_TAKE_PROFIT


def test_same_bar_stop_and_take_is_scored_as_conservative_loss() -> None:
    bars = [
        *history(),
        simple_bar(8, "110", open="108", high="113", low="105"),
    ]
    result = backtester().run(bars)
    assert result.closed_trade_count == 1
    assert result.losing_trades == 1
    assert result.ambiguous_intrabar_exit_count == 1
    trade = result.closed_trades[0]
    assert trade.exit_reason is OhlcvExitReason.INTRABAR_HARD_STOP
    assert trade.exit_execution_price == Decimal("105.84")
    assert trade.net_pnl == Decimal("-2.16")
    assert trade.ambiguous_intrabar_exit is True


def test_gap_through_stop_uses_next_bar_open_not_unreachable_stop_level() -> None:
    bars = [
        *history(),
        simple_bar(8, "108.5", open="108", high="109", low="107"),
        simple_bar(9, "101", open="100", high="102", low="99"),
    ]
    result = backtester().run(bars)
    assert result.closed_trade_count == 1
    assert result.gap_stop_exit_count == 1
    trade = result.closed_trades[0]
    assert trade.exit_reason is OhlcvExitReason.INTRABAR_HARD_STOP
    assert trade.exit_execution_price == Decimal("100")
    assert trade.gap_through_stop is True


def test_trailing_exit_uses_peak_from_completed_prior_bar() -> None:
    bars = [
        *history(),
        simple_bar(8, "108.5", open="108", high="111", low="107"),
        simple_bar(9, "109.5", open="110", high="111", low="109"),
    ]
    result = backtester().run(bars)
    assert result.closed_trade_count == 1
    trade = result.closed_trades[0]
    assert trade.exit_reason is OhlcvExitReason.INTRABAR_TRAILING_STOP
    assert trade.exit_execution_price == Decimal("109.335")
    assert trade.net_pnl == Decimal("1.335")


def test_current_bar_close_cannot_change_entry_decision_before_open() -> None:
    bars = [
        *history(),
        simple_bar(8, "106", open="108", high="108.2", low="105"),
    ]
    result = backtester().run(bars)
    assert result.fill_count == 2
    assert result.closed_trades[0].entry_execution_price == Decimal("108")
    assert result.closed_trades[0].exit_reason is OhlcvExitReason.INTRABAR_HARD_STOP
