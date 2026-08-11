from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.trading import Bar
from app.strategy.managed_backtest import DecisionAction, ManagedHistoricalBacktester
from app.strategy.reentry_confirmation import (
    EntryBlockReason,
    ReentryConfirmationPolicy,
)
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy

START = datetime(2015, 10, 14, tzinfo=UTC)


def rising_sample() -> list[Bar]:
    closes = [
        "110.209999",
        "111.860001",
        "111.040001",
        "111.730003",
        "113.769997",
        "113.760002",
        "115.5",
        "119.080002",
        "115.279999",
        "114.550003",
        "119.269997",
        "120.529999",
        "119.5",
        "121.18",
        "122.57",
        "122",
        "120.919998",
        "121.059998",
        "120.57",
        "116.769997",
    ]
    return [
        Bar("AAPL", START + timedelta(days=index), Decimal(close))
        for index, close in enumerate(closes)
    ]


def test_reentry_confirmation_blocks_one_bar_whipsaw_then_releases() -> None:
    result = ManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
        reentry_policy=ReentryConfirmationPolicy(
            minimum_consecutive_eligible_bars=2
        ),
    ).run(rising_sample(), first_execution_index=10)

    pending = result.decision_trace[4]
    assert pending.execution_index == 14
    assert pending.action is DecisionAction.STAY_FLAT
    assert pending.signal_eligible is True
    assert pending.signal_target_quantity == Decimal("1")
    assert pending.final_target_quantity == Decimal("0")
    assert pending.entry_block_reason is EntryBlockReason.REENTRY_CONFIRMATION_PENDING
    assert pending.reentry_blocked_after_exit is True
    assert pending.reentry_confirmation_streak == 1
    assert pending.reentry_confirmation_required == 2

    confirmed = result.decision_trace[5]
    assert confirmed.execution_index == 15
    assert confirmed.action is DecisionAction.ENTER
    assert confirmed.signal_eligible is True
    assert confirmed.entry_block_reason is None
    assert confirmed.reentry_confirmation_streak == 2
    assert confirmed.reentry_blocked_after_exit is False


def test_confirmation_reduces_observed_second_trade_loss_without_retuning_exits() -> None:
    baseline = ManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
    ).run(rising_sample(), first_execution_index=10)
    candidate = ManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
        reentry_policy=ReentryConfirmationPolicy(
            minimum_consecutive_eligible_bars=2
        ),
    ).run(rising_sample(), first_execution_index=10)

    assert baseline.closed_trade_count == candidate.closed_trade_count == 2
    assert baseline.closed_trades[1].entry_price == Decimal("122.631285")
    assert candidate.closed_trades[1].entry_price == Decimal("122.0610")
    assert baseline.closed_trades[1].net_pnl == Decimal("-2.6318169990")
    assert candidate.closed_trades[1].net_pnl == Decimal("-2.0615319990")
    assert candidate.closed_trades[1].net_pnl > baseline.closed_trades[1].net_pnl
    assert candidate.average_maximum_adverse_excursion_fraction < (
        baseline.average_maximum_adverse_excursion_fraction
    )


def test_default_backtester_keeps_v1_entry_behavior() -> None:
    result = ManagedHistoricalBacktester(
        strategy=RegimeAwareMomentumStrategy(),
    ).run(rising_sample(), first_execution_index=10)
    immediate_reentry = result.decision_trace[4]
    assert immediate_reentry.execution_index == 14
    assert immediate_reentry.action is DecisionAction.ENTER
    assert immediate_reentry.entry_block_reason is None
    assert immediate_reentry.reentry_confirmation_required is None
