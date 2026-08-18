from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionState
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.execution.bybit_demo_trade_management_parity import (
    BybitDemoTradeManagementParityAction,
    evaluate_bybit_demo_trade_management_parity,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide
from app.strategy.crypto_trade_management import CryptoProtectionPolicy


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.1"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("1000"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _excursion(side: CryptoSide = CryptoSide.LONG) -> BybitDemoTradeExcursionState:
    return BybitDemoTradeExcursionState(
        symbol="BTCUSDT",
        side=side,
        entry_price=Decimal("100"),
        initial_quantity=Decimal("2"),
        stop_fraction=Decimal("0.05"),
        current_quantity=Decimal("2"),
    )


def _position(
    *,
    side: str = "Buy",
    stop: str = "95",
    take_profit: str | None = "112",
    trailing: str | None = None,
    size: str = "2",
) -> BybitDemoProtectionPosition:
    return BybitDemoProtectionPosition(
        symbol="BTCUSDT",
        side=side,
        size=Decimal(size),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50") if side == "Buy" else Decimal("150"),
        take_profit_price=None if take_profit is None else Decimal(take_profit),
        stop_loss_price=Decimal(stop),
        trailing_stop_distance=None if trailing is None else Decimal(trailing),
    )


def _bar(
    index: int,
    *,
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> BybitKlineBar:
    start = datetime(2026, 8, 18, tzinfo=UTC) + timedelta(minutes=5 * index)
    return BybitKlineBar(
        symbol="BTCUSDT",
        start_time=start,
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
        turnover=Decimal("1000000"),
    )


def _config() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig()


def test_long_baseline_break_even_ratchet_is_due_after_completed_080r_bar() -> None:
    decision = evaluate_bybit_demo_trade_management_parity(
        _excursion(),
        position=_position(),
        completed_bars_since_entry=(_bar(0, high="104"),),
        strategy_config=_config(),
        instrument=_instrument(),
    )

    assert decision.action is BybitDemoTradeManagementParityAction.RATCHET_BREAK_EVEN
    assert decision.maximum_favorable_r == Decimal("0.8")
    assert decision.desired_stop_loss_price is not None
    assert decision.break_even_price is not None
    assert decision.desired_stop_loss_price == decision.break_even_price
    assert decision.desired_stop_loss_price > Decimal("100")
    assert decision.current_stop_loss_price == Decimal("95")
    assert decision.stop_never_widens is True
    assert decision.tight_profit_lock_candidate_allowed is False
    assert decision.demo_stop_ratchet_write_allowed is False


def test_long_baseline_profit_lock_ratchets_to_point_35r_after_125r() -> None:
    decision = evaluate_bybit_demo_trade_management_parity(
        _excursion(),
        position=_position(),
        completed_bars_since_entry=(_bar(0, high="106.25"),),
        strategy_config=_config(),
        instrument=_instrument(),
    )

    assert decision.action is BybitDemoTradeManagementParityAction.RATCHET_PROFIT_LOCK
    assert decision.maximum_favorable_r == Decimal("1.25")
    assert decision.desired_stop_loss_price == Decimal("101.7")
    assert decision.desired_stop_reason == "PROFIT_PROTECTION"


def test_short_baseline_profit_lock_moves_stop_only_in_protective_direction() -> None:
    decision = evaluate_bybit_demo_trade_management_parity(
        _excursion(CryptoSide.SHORT),
        position=_position(
            side="Sell",
            stop="105",
            take_profit="88",
        ),
        completed_bars_since_entry=(_bar(0, low="93.75"),),
        strategy_config=_config(),
        instrument=_instrument(),
    )

    assert decision.action is BybitDemoTradeManagementParityAction.RATCHET_PROFIT_LOCK
    assert decision.maximum_favorable_r == Decimal("1.25")
    assert decision.desired_stop_loss_price == Decimal("98.3")
    assert decision.desired_stop_loss_price < Decimal("105")


def test_existing_more_protective_stop_is_never_widened() -> None:
    decision = evaluate_bybit_demo_trade_management_parity(
        _excursion(),
        position=_position(stop="102"),
        completed_bars_since_entry=(_bar(0, high="104"),),
        strategy_config=_config(),
        instrument=_instrument(),
    )

    assert decision.action is BybitDemoTradeManagementParityAction.NO_CHANGE
    assert decision.desired_stop_loss_price is not None
    assert decision.desired_stop_loss_price < Decimal("102")


def test_runner_keeps_native_trailing_while_baseline_hard_stop_can_ratchet() -> None:
    decision = evaluate_bybit_demo_trade_management_parity(
        _excursion(),
        position=_position(
            stop="95",
            take_profit=None,
            trailing="1.5",
        ),
        completed_bars_since_entry=(_bar(0, high="104"),),
        strategy_config=_config(),
        instrument=_instrument(),
    )

    assert decision.action is BybitDemoTradeManagementParityAction.RATCHET_BREAK_EVEN
    assert decision.exit_mode == "OPEN_ENDED_RUNNER"
    assert decision.runner_trailing_preserved is True
    assert decision.fixed_take_profit_preserved is False


def test_fixed_target_shape_is_preserved() -> None:
    decision = evaluate_bybit_demo_trade_management_parity(
        _excursion(),
        position=_position(),
        completed_bars_since_entry=(_bar(0, high="104"),),
        strategy_config=_config(),
        instrument=_instrument(),
    )

    assert decision.exit_mode == "FIXED_20_TARGET"
    assert decision.fixed_take_profit_preserved is True
    assert decision.runner_trailing_preserved is False


def test_max_hold_is_detected_from_same_baseline_policy() -> None:
    bars = tuple(_bar(index) for index in range(36))
    decision = evaluate_bybit_demo_trade_management_parity(
        _excursion(),
        position=_position(),
        completed_bars_since_entry=bars,
        strategy_config=_config(),
        instrument=_instrument(),
    )

    assert decision.action is BybitDemoTradeManagementParityAction.MAX_HOLD_CLOSE_REQUIRED
    assert decision.completed_bar_count == 36
    assert decision.maximum_holding_bars == 36
    assert decision.max_hold_close_required is True


def test_partial_or_changed_position_size_blocks_baseline_parity() -> None:
    decision = evaluate_bybit_demo_trade_management_parity(
        _excursion(),
        position=_position(size="1"),
        completed_bars_since_entry=(_bar(0, high="104"),),
        strategy_config=_config(),
        instrument=_instrument(),
    )

    assert decision.action is BybitDemoTradeManagementParityAction.BLOCKED
    assert "PARTIAL_OR_CHANGED_POSITION_SIZE_NOT_BASELINE_PARITY" in decision.reasons


def test_non_monotonic_completed_bars_are_rejected() -> None:
    decision = evaluate_bybit_demo_trade_management_parity(
        _excursion(),
        position=_position(),
        completed_bars_since_entry=(_bar(1), _bar(0)),
        strategy_config=_config(),
        instrument=_instrument(),
    )

    assert decision.action is BybitDemoTradeManagementParityAction.BLOCKED
    assert decision.reasons == ("COMPLETED_BARS_NOT_STRICTLY_INCREASING",)


def test_rejected_tight_profit_lock_policy_cannot_enter_demo_parity() -> None:
    tight = CryptoProtectionPolicy(
        break_even_activation_r=Decimal("0.8"),
        profit_lock_activation_r=Decimal("1.0"),
        profit_lock_r=Decimal("0.5"),
    )

    with pytest.raises(ValueError, match="frozen baseline protection policy"):
        evaluate_bybit_demo_trade_management_parity(
            _excursion(),
            position=_position(),
            completed_bars_since_entry=(_bar(0, high="105"),),
            strategy_config=_config(),
            instrument=_instrument(),
            protection_policy=tight,
        )
