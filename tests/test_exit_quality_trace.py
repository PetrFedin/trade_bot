from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.trading import Bar
from app.strategy.managed_backtest import DecisionAction, ManagedHistoricalBacktester
from app.strategy.position_management import ExitReason, PositionManagementPolicy
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy

START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


def series(values: list[str]) -> list[Bar]:
    return [
        Bar("AAPL", START + timedelta(minutes=index), Decimal(value))
        for index, value in enumerate(values)
    ]


def test_take_profit_records_close_only_excursions_and_capture() -> None:
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
    trade = result.closed_trades[0]
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.maximum_favorable_excursion_fraction == Decimal("7") / Decimal("108")
    assert trade.maximum_adverse_excursion_fraction == Decimal("0")
    assert trade.mfe_capture_ratio == Decimal("1")
    assert trade.mfe_giveback_fraction == Decimal("0")
    assert result.profit_preservation_rate == Decimal("1")


def test_decision_trace_shows_position_manager_overriding_bullish_signal() -> None:
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
    assert [item.action for item in result.decision_trace] == [
        DecisionAction.ENTER,
        DecisionAction.HOLD,
        DecisionAction.HOLD,
        DecisionAction.EXIT,
    ]
    exit_decision = result.decision_trace[-1]
    assert exit_decision.signal_eligible is True
    assert exit_decision.signal_target_quantity == Decimal("1")
    assert exit_decision.final_target_quantity == Decimal("0")
    assert exit_decision.exit_reason is ExitReason.TAKE_PROFIT
    assert exit_decision.position_profit_fraction == Decimal("6") / Decimal("108")


def test_losing_exit_after_positive_excursion_is_visible_as_profit_giveback() -> None:
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
                "110",
                "105",
                "104",
            ]
        )
    )
    trade = result.closed_trades[0]
    assert trade.maximum_favorable_excursion_fraction == Decimal("2") / Decimal("108")
    assert trade.maximum_adverse_excursion_fraction == Decimal("4") / Decimal("108")
    assert trade.net_pnl == Decimal("-4")
    assert trade.mfe_capture_ratio == Decimal("-2")
    assert trade.mfe_giveback_fraction == Decimal("3")
    assert result.positive_mfe_trades == 1
    assert result.positive_mfe_closed_losing_or_flat == 1
    assert result.profit_preservation_rate == Decimal("0")


def test_profit_protection_can_convert_confirmed_mfe_into_realized_gain() -> None:
    result = ManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
        position_policy=PositionManagementPolicy(
            trailing_activation_fraction=Decimal("0.03"),
            profit_protection_activation_fraction=Decimal("0.01"),
            maximum_profit_giveback_fraction=Decimal("0.50"),
        ),
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
                "110",
                "109",
                "108.8",
            ]
        )
    )
    trade = result.closed_trades[0]
    assert trade.exit_reason is ExitReason.PROFIT_PROTECTION
    assert trade.net_pnl == Decimal("0.8")
    assert trade.maximum_favorable_excursion_fraction == Decimal("2") / Decimal("108")
    assert trade.mfe_capture_ratio == Decimal("0.4")
    assert result.positive_mfe_closed_profitable == 1
    assert result.profit_preservation_rate == Decimal("1")


def test_flat_decision_trace_preserves_rejection_reasons() -> None:
    result = ManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
    ).run(
        series(
            [
                "100",
                "101",
                "100",
                "101",
                "100",
                "101",
                "100",
                "120",
                "119",
            ]
        )
    )
    assert len(result.decision_trace) == 1
    decision = result.decision_trace[0]
    assert decision.action is DecisionAction.STAY_FLAT
    assert decision.signal_eligible is False
    assert "REALIZED_VOLATILITY_ABOVE_LIMIT" in decision.signal_reasons
