from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.trading import Bar
from app.strategy.backtest import BacktestConfig, HistoricalBacktester
from app.strategy.momentum import LongOnlyMomentumStrategy

UTC = timezone.utc
START = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)


def series(closes: list[str]) -> list[Bar]:
    return [
        Bar("AAPL", START + timedelta(minutes=index), Decimal(close))
        for index, close in enumerate(closes)
    ]


def test_backtest_executes_on_next_bar_without_same_bar_lookahead() -> None:
    result = HistoricalBacktester(
        strategy=LongOnlyMomentumStrategy(target_quantity=Decimal("1"))
    ).run(series(["100", "101", "102", "103", "104"]))
    assert result.trades == 1
    # Signal appears on the 102 close but the fill occurs on the next bar at 103.
    assert result.total_pnl == Decimal("1")
    assert result.ending_equity == Decimal("10001")
    assert result.final_quantity == Decimal("1")


def test_backtest_models_fees_and_adverse_slippage() -> None:
    result = HistoricalBacktester(
        strategy=LongOnlyMomentumStrategy(target_quantity=Decimal("1")),
        config=BacktestConfig(
            opening_cash=Decimal("10000"),
            fee_per_fill=Decimal("1"),
            slippage_bps=Decimal("100"),
        ),
    ).run(series(["100", "101", "102", "103", "104"]))
    assert result.trades == 1
    assert result.fees_paid == Decimal("1")
    assert result.total_pnl == Decimal("-0.03")
    assert result.total_return == Decimal("-0.000003")


def test_backtest_can_exit_and_records_drawdown() -> None:
    result = HistoricalBacktester(
        strategy=LongOnlyMomentumStrategy(target_quantity=Decimal("1"))
    ).run(series(["100", "101", "103", "102", "90", "80"]))
    assert result.trades == 2
    assert result.final_quantity == Decimal("0")
    assert result.total_pnl == Decimal("-22")
    assert result.max_drawdown >= Decimal("22")
