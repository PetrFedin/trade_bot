from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.bybit_demo import (
    BybitDemoOrderAck,
    BybitDemoPosition,
    BybitDemoProtectionAck,
    BybitDemoRunnerProtectionAck,
)
from app.execution.bybit_demo_cycle import (
    BybitDemoCycleResult,
    BybitDemoCycleStatus,
)
from app.execution.bybit_demo_orchestrator import (
    BybitDemoOrchestratorResult,
    BybitDemoOrchestratorStatus,
)
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.execution.bybit_demo_protection_reconciliation import (
    BybitDemoProtectionReconciliationPolicy,
    evaluate_bybit_demo_exchange_protection,
    execute_protection_reconciled_guarded_bybit_demo_cycle,
    reconcile_bybit_demo_exchange_protection,
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
    take_profit: Decimal | None = Decimal("102000"),
    stop_loss: Decimal | None = Decimal("99500"),
    trailing: Decimal | None = None,
) -> BybitDemoProtectionPosition:
    return BybitDemoProtectionPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal("0.01"),
        average_price=Decimal("100000"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("97000"),
        take_profit_price=take_profit,
        stop_loss_price=stop_loss,
        trailing_stop_distance=trailing,
    )


def _fixed_ack() -> BybitDemoProtectionAck:
    return BybitDemoProtectionAck(
        symbol="BTCUSDT",
        take_profit_price=Decimal("102000"),
        stop_loss_price=Decimal("99500"),
    )


def _runner_ack() -> BybitDemoRunnerProtectionAck:
    return BybitDemoRunnerProtectionAck(
        symbol="BTCUSDT",
        stop_loss_price=Decimal("99500"),
        trailing_stop_distance=Decimal("400"),
        trailing_active_price=Decimal("102000"),
    )


def _base_position() -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal("0.01"),
        average_price=Decimal("100000"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("97000"),
    )


def _protected_cycle(ack: object) -> BybitDemoCycleResult:
    return BybitDemoCycleResult(
        status=BybitDemoCycleStatus.PROTECTED,
        reasons=(),
        entry_ack=BybitDemoOrderAck(
            order_id="entry-1",
            order_link_id="ASTRA-DEMO-E-VERIFY",
            accepted=True,
        ),
        protection_ack=ack,
        flatten_ack=None,
        reconciled_position=_base_position(),
        next_entry_allowed=True,
        demo_order_writes_enabled=True,
        exit_mode=(
            "OPEN_ENDED_RUNNER"
            if isinstance(ack, BybitDemoRunnerProtectionAck)
            else "FIXED_20_TARGET"
        ),
    )


def _base_orchestrator_result(ack: object) -> BybitDemoOrchestratorResult:
    return BybitDemoOrchestratorResult(
        status=BybitDemoOrchestratorStatus.CYCLE_EXECUTED,
        reasons=(),
        cycle_result=_protected_cycle(ack),
        previous_trade_gate_checked=False,
        next_entry_allowed=True,
    )


class _Client:
    protection_state_read_supported = True
    live_mainnet_order_routing_allowed = False

    def __init__(self, snapshots: list[object]) -> None:
        self.snapshots = snapshots
        self.reads = 0
        self.orders: list[object] = []

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        value = self.snapshots[min(self.reads, len(self.snapshots) - 1)]
        self.reads += 1
        if isinstance(value, Exception):
            raise value
        return value

    def place_market_order(self, request: object) -> BybitDemoOrderAck:
        self.orders.append(request)
        return BybitDemoOrderAck(
            order_id=f"flatten-{len(self.orders)}",
            order_link_id=request.order_link_id,
            accepted=True,
        )


def test_fixed_protection_requires_exact_tp_and_sl_and_no_trailing() -> None:
    decision = evaluate_bybit_demo_exchange_protection(
        (_position(),),
        trade_plan=_trade_plan(),
        protection_ack=_fixed_ack(),
    )

    assert decision.reconciled is True
    assert decision.reason == "VERIFIED"
    assert decision.runner_active_price_observable is False


def test_runner_protection_checks_stop_trailing_and_absence_of_fixed_tp() -> None:
    decision = evaluate_bybit_demo_exchange_protection(
        (_position(take_profit=None, trailing=Decimal("400")),),
        trade_plan=_trade_plan(),
        protection_ack=_runner_ack(),
    )

    assert decision.reconciled is True
    assert decision.reason == "VERIFIED"
    assert decision.runner_active_price_observable is False


def test_protection_reconciliation_retries_eventual_consistency() -> None:
    client = _Client(
        [
            (_position(stop_loss=Decimal("99400")),),
            (_position(),),
        ]
    )
    decision = reconcile_bybit_demo_exchange_protection(
        client=client,
        trade_plan=_trade_plan(),
        protection_ack=_fixed_ack(),
        policy=BybitDemoProtectionReconciliationPolicy(attempts=2, delay_seconds=0),
    )

    assert decision.reconciled is True
    assert decision.attempts_used == 2
    assert client.reads == 2


def test_unverified_exchange_protection_triggers_reduce_only_flatten() -> None:
    client = _Client([(_position(stop_loss=Decimal("99400")),)])

    result = execute_protection_reconciled_guarded_bybit_demo_cycle(
        _trade_plan(),
        instrument=_instrument(),
        client=client,
        protection_reconciliation_policy=BybitDemoProtectionReconciliationPolicy(
            attempts=1,
            delay_seconds=0,
        ),
        base_orchestrator=lambda *_args, **_kwargs: _base_orchestrator_result(
            _fixed_ack()
        ),
    )

    assert result.protection_state_checked is True
    assert result.protection_state_reconciled is False
    assert result.protection_state_reason == "STOP_LOSS_MISMATCH"
    assert result.next_entry_allowed is False
    assert result.cycle_result is not None
    assert result.cycle_result.status is BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED
    assert result.cycle_result.flatten_ack is not None
    assert len(client.orders) == 1
    assert client.orders[0].reduce_only is True


def test_protection_read_failure_also_flattens_fail_closed() -> None:
    client = _Client([TimeoutError("position-state")])

    result = execute_protection_reconciled_guarded_bybit_demo_cycle(
        _trade_plan(),
        instrument=_instrument(),
        client=client,
        protection_reconciliation_policy=BybitDemoProtectionReconciliationPolicy(
            attempts=1,
            delay_seconds=0,
        ),
        base_orchestrator=lambda *_args, **_kwargs: _base_orchestrator_result(
            _fixed_ack()
        ),
    )

    assert result.protection_state_reconciled is False
    assert result.protection_state_reason == "PROTECTION_STATE_READ_FAILED:TimeoutError"
    assert result.cycle_result is not None
    assert result.cycle_result.status is BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED
    assert client.orders[0].reduce_only is True


def test_verified_protection_preserves_protected_cycle_without_extra_order() -> None:
    client = _Client([(_position(),)])

    result = execute_protection_reconciled_guarded_bybit_demo_cycle(
        _trade_plan(),
        instrument=_instrument(),
        client=client,
        protection_reconciliation_policy=BybitDemoProtectionReconciliationPolicy(
            attempts=1,
            delay_seconds=0,
        ),
        base_orchestrator=lambda *_args, **_kwargs: _base_orchestrator_result(
            _fixed_ack()
        ),
    )

    assert result.protection_state_checked is True
    assert result.protection_state_reconciled is True
    assert result.protection_state_reason == "VERIFIED"
    assert result.next_entry_allowed is True
    assert result.cycle_result is not None
    assert result.cycle_result.status is BybitDemoCycleStatus.PROTECTED
    assert client.orders == []


def test_canonical_protection_orchestrator_rejects_unverifiable_client_before_base_call() -> None:
    called = False

    def base(*_args: object, **_kwargs: object) -> BybitDemoOrchestratorResult:
        nonlocal called
        called = True
        return _base_orchestrator_result(_fixed_ack())

    with pytest.raises(ValueError, match="protection-state read capability"):
        execute_protection_reconciled_guarded_bybit_demo_cycle(
            _trade_plan(),
            instrument=_instrument(),
            client=object(),
            base_orchestrator=base,
        )

    assert called is False
