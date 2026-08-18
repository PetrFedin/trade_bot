from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoPosition, BybitDemoProtectionAck
from app.execution.bybit_demo_cycle import BybitDemoCycleResult, BybitDemoCycleStatus
from app.execution.bybit_demo_orchestrator import (
    BybitDemoOrchestratorResult,
    BybitDemoOrchestratorStatus,
)
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.execution.bybit_demo_protection_reconciliation import (
    BybitDemoProtectionReconciliationPolicy,
    execute_protection_reconciled_guarded_bybit_demo_cycle,
    reconcile_bybit_demo_emergency_flatten,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


def _trade_plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-18T20:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1000"),
        reference_quantity=Decimal("0.01"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.005"),
        estimated_round_trip_cost_usdt=Decimal("1.6"),
        estimated_stop_loss_after_cost_usdt=Decimal("6.6"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.0216"),
        expected_move_fraction=Decimal("0.03"),
        expected_net_edge_usd=Decimal("28.4"),
        quality_score=Decimal("2"),
    )


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


def _position(
    *,
    size: Decimal = Decimal("0.01"),
    stop_loss: Decimal = Decimal("99400"),
) -> BybitDemoProtectionPosition:
    return BybitDemoProtectionPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=size,
        average_price=Decimal("100000"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("97000"),
        take_profit_price=Decimal("102000"),
        stop_loss_price=stop_loss,
        trailing_stop_distance=None,
    )


def _base_cycle() -> BybitDemoCycleResult:
    return BybitDemoCycleResult(
        status=BybitDemoCycleStatus.PROTECTED,
        reasons=(),
        entry_ack=BybitDemoOrderAck(
            order_id="entry-1",
            order_link_id="ASTRA-DEMO-E-FLAT",
            accepted=True,
        ),
        protection_ack=BybitDemoProtectionAck(
            symbol="BTCUSDT",
            take_profit_price=Decimal("102000"),
            stop_loss_price=Decimal("99500"),
        ),
        flatten_ack=None,
        reconciled_position=BybitDemoPosition(
            symbol="BTCUSDT",
            side="Buy",
            size=Decimal("0.01"),
            average_price=Decimal("100000"),
            unrealised_pnl=Decimal("0"),
            liquidation_price=Decimal("97000"),
        ),
        next_entry_allowed=True,
        demo_order_writes_enabled=True,
        exit_mode="FIXED_20_TARGET",
    )


def _base_result() -> BybitDemoOrchestratorResult:
    return BybitDemoOrchestratorResult(
        status=BybitDemoOrchestratorStatus.CYCLE_EXECUTED,
        reasons=(),
        cycle_result=_base_cycle(),
        previous_trade_gate_checked=False,
        next_entry_allowed=True,
    )


class _Client:
    protection_state_read_supported = True
    live_mainnet_order_routing_allowed = False

    def __init__(self, snapshots: list[object]) -> None:
        self.snapshots = snapshots
        self.read_index = 0
        self.orders: list[object] = []

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        index = min(self.read_index, len(self.snapshots) - 1)
        value = self.snapshots[index]
        self.read_index += 1
        if isinstance(value, Exception):
            raise value
        return value

    def place_market_order(self, request: object) -> BybitDemoOrderAck:
        self.orders.append(request)
        return BybitDemoOrderAck(
            order_id="flatten-1",
            order_link_id=request.order_link_id,
            accepted=True,
        )


def _policy(*, flatten_attempts: int = 2) -> BybitDemoProtectionReconciliationPolicy:
    return BybitDemoProtectionReconciliationPolicy(
        attempts=1,
        delay_seconds=0,
        flatten_attempts=flatten_attempts,
        flatten_delay_seconds=0,
    )


def test_emergency_flatten_confirmation_accepts_eventual_position_disappearance() -> None:
    client = _Client([(_position(size=Decimal("0.004")),), ()])

    decision = reconcile_bybit_demo_emergency_flatten(
        client=client,
        trade_plan=_trade_plan(),
        policy=_policy(flatten_attempts=2),
    )

    assert decision.position_closed is True
    assert decision.reason == "EMERGENCY_FLATTEN_CONFIRMED_CLOSED"
    assert decision.attempts_used == 2
    assert decision.residual_size == Decimal("0")


def test_emergency_flatten_confirmation_surfaces_residual_size() -> None:
    client = _Client(
        [
            (_position(size=Decimal("0.006")),),
            (_position(size=Decimal("0.002")),),
        ]
    )

    decision = reconcile_bybit_demo_emergency_flatten(
        client=client,
        trade_plan=_trade_plan(),
        policy=_policy(flatten_attempts=2),
    )

    assert decision.position_closed is False
    assert decision.reason == "EMERGENCY_FLATTEN_RESIDUAL_POSITION"
    assert decision.attempts_used == 2
    assert decision.residual_size == Decimal("0.002")


def test_emergency_flatten_confirmation_surfaces_unreadable_position_state() -> None:
    client = _Client([TimeoutError("position-state")])

    decision = reconcile_bybit_demo_emergency_flatten(
        client=client,
        trade_plan=_trade_plan(),
        policy=_policy(flatten_attempts=2),
    )

    assert decision.position_closed is False
    assert decision.reason == "EMERGENCY_FLATTEN_POSITION_READ_FAILED:TimeoutError"
    assert decision.residual_size is None


def test_protection_mismatch_flatten_is_confirmed_from_position_state() -> None:
    client = _Client([(_position(),), ()])

    result = execute_protection_reconciled_guarded_bybit_demo_cycle(
        _trade_plan(),
        instrument=_instrument(),
        client=client,
        protection_reconciliation_policy=_policy(flatten_attempts=1),
        base_orchestrator=lambda *_args, **_kwargs: _base_result(),
    )

    assert len(client.orders) == 1
    assert client.orders[0].reduce_only is True
    assert result.next_entry_allowed is False
    assert result.emergency_flatten_requested is True
    assert result.emergency_flatten_position_closed is True
    assert result.emergency_flatten_reconciliation_attempts == 1
    assert result.emergency_flatten_residual_size == Decimal("0")
    assert (
        result.emergency_flatten_reconciliation_reason
        == "EMERGENCY_FLATTEN_CONFIRMED_CLOSED"
    )


def test_protection_mismatch_flatten_keeps_residual_position_observable() -> None:
    client = _Client(
        [
            (_position(size=Decimal("0.006")),),
            (_position(size=Decimal("0.002")),),
        ]
    )

    result = execute_protection_reconciled_guarded_bybit_demo_cycle(
        _trade_plan(),
        instrument=_instrument(),
        client=client,
        protection_reconciliation_policy=_policy(flatten_attempts=1),
        base_orchestrator=lambda *_args, **_kwargs: _base_result(),
    )

    assert result.emergency_flatten_requested is True
    assert result.emergency_flatten_position_closed is False
    assert result.emergency_flatten_residual_size == Decimal("0.002")
    assert (
        result.emergency_flatten_reconciliation_reason
        == "EMERGENCY_FLATTEN_RESIDUAL_POSITION"
    )
    assert result.next_entry_allowed is False


def test_emergency_flatten_confirmation_ignores_other_symbols_and_sides() -> None:
    other = SimpleNamespace(
        symbol="ETHUSDT",
        side="Buy",
        size=Decimal("2"),
    )
    opposite = SimpleNamespace(
        symbol="BTCUSDT",
        side="Sell",
        size=Decimal("0.01"),
    )
    client = _Client([(other, opposite)])

    decision = reconcile_bybit_demo_emergency_flatten(
        client=client,
        trade_plan=_trade_plan(),
        policy=_policy(flatten_attempts=1),
    )

    assert decision.position_closed is True
    assert decision.residual_size == Decimal("0")
