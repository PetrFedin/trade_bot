from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.trading import Bar
from app.strategy.managed_backtest import ManagedHistoricalBacktester
from app.strategy.position_management import ExitReason, PositionManagementPolicy
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy

UTC = timezone.utc
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


def series(values: list[str]) -> list[Bar]:
    return [
        Bar("AAPL", START + timedelta(minutes=index), Decimal(value))
        for index, value in enumerate(values)
    ]


def test_take_profit_is_decided_from_prior_close_and_filled_next_bar() -> None:
    result = ManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
    ).run(
        series(
            [
                "100",
                "101",
                "102",
                "103",
                "104",
                "105",
                "106",
                "107",
                "108",
                "109",
                "114",
                "115",
            ]
        )
    )
    assert result.fill_count == 2
    assert result.closed_trade_count == 1
    assert result.winning_trades == 1
    assert result.win_rate == Decimal("1")
    trade = result.closed_trades[0]
    assert trade.entry_price == Decimal("108")
    assert trade.exit_price == Decimal("115")
    assert trade.net_pnl == Decimal("7")
    assert trade.exit_reason is ExitReason.TAKE_PROFIT


def test_stop_loss_overrides_signal_exit_reason_after_adverse_move() -> None:
    result = ManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
    ).run(
        series(
            [
                "100",
                "101",
                "102",
                "103",
                "104",
                "105",
                "106",
                "107",
                "108",
                "104",
                "103",
            ]
        )
    )
    assert result.closed_trade_count == 1
    assert result.losing_trades == 1
    trade = result.closed_trades[0]
    assert trade.entry_price == Decimal("108")
    assert trade.exit_price == Decimal("103")
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.net_pnl == Decimal("-5")


def test_time_stop_closes_stale_position_and_records_trade_quality() -> None:
    policy = PositionManagementPolicy(
        stop_loss_fraction=Decimal("0.50"),
        take_profit_fraction=Decimal("0.50"),
        trailing_activation_fraction=Decimal("0.50"),
        trailing_stop_fraction=Decimal("0.10"),
        maximum_holding_bars=2,
    )
    result = ManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
        position_policy=policy,
    ).run(
        series(
            [
                "100",
                "101",
                "102",
                "103",
                "104",
                "105",
                "106",
                "107",
                "108",
                "109",
                "110",
                "111",
            ]
        )
    )
    assert result.closed_trade_count == 1
    assert result.closed_trades[0].exit_reason is ExitReason.TIME_STOP
    assert result.closed_trades[0].holding_bars == 2
    assert result.average_closed_trade_pnl == result.closed_trades[0].net_pnl
