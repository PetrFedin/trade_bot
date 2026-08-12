from decimal import Decimal

from app.execution.bybit_demo_controller import (
    plan_bybit_demo_entry,
    plan_bybit_demo_protection_after_fill,
    plan_bybit_demo_reduce_only_close,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoTradePlan,
)
from app.strategy.crypto_session_risk import CryptoSessionRiskState


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.10"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("500"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _trade_plan(side: CryptoSide = CryptoSide.LONG) -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=side,
        decision_time="2026-08-12T18:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1200"),
        reference_quantity=Decimal("0.01249"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.004"),
        estimated_round_trip_cost_usdt=Decimal("1.92"),
        estimated_stop_loss_after_cost_usdt=Decimal("6.72"),
        target_net_profit_usd=Decimal("15"),
        required_move_fraction=Decimal("0.0141"),
        expected_move_fraction=Decimal("0.015"),
        expected_net_edge_usd=Decimal("16.08"),
        quality_score=Decimal("2.5"),
    )


def _healthy_session() -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("1005"),
        peak_equity_usdt=Decimal("1010"),
        realized_pnl_usdt=Decimal("5"),
        execution_cost_usdt=Decimal("4"),
        consecutive_losses=0,
    )


def test_demo_entry_is_quantized_down_and_requires_post_fill_protection() -> None:
    planned = plan_bybit_demo_entry(
        _trade_plan(),
        instrument=_instrument(),
        session_state=_healthy_session(),
    )

    assert planned.eligible is True
    assert planned.order is not None
    assert planned.order.side == "Buy"
    assert planned.order.quantity == Decimal("0.012")
    assert planned.order.reduce_only is False
    assert planned.order.order_link_id.startswith("ASTRA-DEMO-E-")
    assert planned.protection_after_fill_required is True
    assert planned.live_mainnet_order_routing_allowed is False


def test_session_guard_blocks_demo_entry_before_order_creation() -> None:
    blocked_session = CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("950"),
        peak_equity_usdt=Decimal("1000"),
        realized_pnl_usdt=Decimal("-20"),
        execution_cost_usdt=Decimal("5"),
        consecutive_losses=2,
    )

    planned = plan_bybit_demo_entry(
        _trade_plan(),
        instrument=_instrument(),
        session_state=blocked_session,
    )

    assert planned.eligible is False
    assert planned.order is None
    assert "SESSION_DRAWDOWN_LIMIT_BREACHED" in planned.reasons
    assert planned.live_mainnet_order_routing_allowed is False


def test_post_fill_protection_uses_actual_fill_and_exchange_ticks() -> None:
    planned = plan_bybit_demo_protection_after_fill(
        _trade_plan(),
        actual_average_entry_price=Decimal("100050.07"),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
    )

    assert planned.eligible is True
    assert planned.protection is not None
    assert planned.protection.average_entry_price == Decimal("100050.07")
    assert planned.protection.take_profit_price % Decimal("0.10") == 0
    assert planned.protection.stop_loss_price % Decimal("0.10") == 0
    assert planned.protection.stop_loss_price < Decimal("100050.07")
    assert planned.protection.take_profit_price > Decimal("100050.07")
    assert planned.live_mainnet_order_routing_allowed is False


def test_emergency_close_is_opposite_side_and_reduce_only() -> None:
    close = plan_bybit_demo_reduce_only_close(
        _trade_plan(CryptoSide.SHORT),
        open_quantity=Decimal("0.0129"),
        instrument=_instrument(),
    )

    assert close.side == "Buy"
    assert close.quantity == Decimal("0.012")
    assert close.reduce_only is True
    assert close.order_link_id.startswith("ASTRA-DEMO-C-")
