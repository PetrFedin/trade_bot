from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.execution.bybit_demo import (
    BybitDemoOrderAck,
    BybitDemoPosition,
    BybitDemoProtectionAck,
    BybitDemoProtectionRequest,
    BybitDemoRunnerProtectionAck,
    BybitDemoRunnerProtectionRequest,
)
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.execution.bybit_entry_recovery import (
    BybitEntryRecoveryEnvelope,
    BybitEntryRecoveryRecord,
    encode_entry_recovery_envelope,
)
from app.execution.bybit_entry_restart_recovery import (
    BybitExecutedEntryRecoveryAction,
    BybitExecutedEntryRecoveryStatus,
    build_recovered_entry_excursion_state,
    execute_bybit_executed_entry_recovery,
    plan_bybit_executed_entry_recovery,
)
from app.execution.bybit_order_lookup import BybitOrderTruth
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan

ENTRY_LINK = "ASTRA-DEMO-E-RECOVER-0001"


def _plan(*, risk_budget: str = "20") -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-21T12:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1000"),
        reference_quantity=Decimal("0.01"),
        risk_budget_usdt=Decimal(risk_budget),
        stop_fraction=Decimal("0.01"),
        estimated_round_trip_cost_usdt=Decimal("1.10"),
        estimated_stop_loss_after_cost_usdt=Decimal("11.10"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.0211"),
        expected_move_fraction=Decimal("0.05"),
        expected_net_edge_usd=Decimal("48.90"),
        quality_score=Decimal("0.91"),
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
        max_market_order_qty=Decimal("100"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _record(
    *,
    planned_exit_mode: str = "FIXED_20_TARGET",
    risk_budget: str = "20",
) -> BybitEntryRecoveryRecord:
    envelope = BybitEntryRecoveryEnvelope(
        entry_order_link_id=ENTRY_LINK,
        order_side="Buy",
        approved_order_quantity=Decimal("0.01"),
        trade_plan=_plan(risk_budget=risk_budget),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(taker_fee_rate=Decimal("0.00055")),
        planned_exit_mode=planned_exit_mode,
    )
    _canonical, record_sha = encode_entry_recovery_envelope(envelope)
    return BybitEntryRecoveryRecord(envelope=envelope, record_sha256=record_sha)


def _truth(*, executed: str = "0.01") -> BybitOrderTruth:
    return BybitOrderTruth(
        order_id="broker-entry-1",
        order_link_id=ENTRY_LINK,
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("0.01"),
        cumulative_executed_quantity=Decimal(executed),
        status="Filled",
        reject_reason="EC_NoError",
    )


def _position(*, average_price: str = "100000", size: str = "0.01") -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal(size),
        average_price=Decimal(average_price),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50000"),
    )


class _RecoveryClient:
    live_mainnet_order_routing_allowed = False
    protection_state_read_supported = True

    def __init__(self, position: BybitDemoPosition, *, verify_protection: bool = True) -> None:
        self.position: BybitDemoPosition | None = position
        self.verify_protection = verify_protection
        self.events: list[str] = []
        self.order_requests = []

    def set_full_position_protection(
        self,
        request: BybitDemoProtectionRequest,
    ) -> BybitDemoProtectionAck:
        self.events.append("protect-fixed")
        ack = BybitDemoProtectionAck(
            symbol=request.symbol,
            take_profit_price=request.take_profit_price,
            stop_loss_price=request.stop_loss_price,
        )
        self._apply_protection(
            take_profit=request.take_profit_price,
            stop_loss=request.stop_loss_price,
            trailing=None,
        )
        return ack

    def set_open_ended_position_protection(
        self,
        request: BybitDemoRunnerProtectionRequest,
    ) -> BybitDemoRunnerProtectionAck:
        self.events.append("protect-runner")
        ack = BybitDemoRunnerProtectionAck(
            symbol=request.symbol,
            stop_loss_price=request.stop_loss_price,
            trailing_stop_distance=request.trailing_stop_distance,
            trailing_active_price=request.trailing_active_price,
        )
        self._apply_protection(
            take_profit=None,
            stop_loss=request.stop_loss_price,
            trailing=request.trailing_stop_distance,
        )
        return ack

    def place_market_order(self, request):
        self.events.append("reduce-only-close")
        self.order_requests.append(request)
        assert request.reduce_only is True
        self.position = None
        return BybitDemoOrderAck("broker-close-1", request.order_link_id, True)

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        self.events.append("read-position")
        return () if self.position is None else (self.position,)

    def _apply_protection(self, *, take_profit, stop_loss, trailing) -> None:
        assert self.position is not None
        actual_stop = stop_loss if self.verify_protection else stop_loss + Decimal("1")
        self.position = BybitDemoProtectionPosition(
            symbol=self.position.symbol,
            side=self.position.side,
            size=self.position.size,
            average_price=self.position.average_price,
            unrealised_pnl=self.position.unrealised_pnl,
            liquidation_price=self.position.liquidation_price,
            take_profit_price=take_profit,
            stop_loss_price=actual_stop,
            trailing_stop_distance=trailing,
        )


def test_fixed_recovery_never_upgrades_to_runner_and_builds_checkpoint_state() -> None:
    plan = plan_bybit_executed_entry_recovery(
        _record(planned_exit_mode="FIXED_20_TARGET"),
        order_truth=_truth(),
        positions=(_position(),),
    )

    assert plan.action is BybitExecutedEntryRecoveryAction.PROTECT
    assert plan.exit_mode == "FIXED_20_TARGET"
    assert isinstance(plan.protection_request, BybitDemoProtectionRequest)

    client = _RecoveryClient(plan.position)
    result = execute_bybit_executed_entry_recovery(plan, client=client)

    assert result.status is BybitExecutedEntryRecoveryStatus.PROTECTED
    assert client.events == ["protect-fixed", "read-position"]
    assert client.order_requests == []
    state = build_recovered_entry_excursion_state(result)
    assert state.symbol == "BTCUSDT"
    assert state.side is CryptoSide.LONG
    assert state.entry_price == Decimal("100000")
    assert state.initial_quantity == Decimal("0.01")


def test_frozen_runner_can_remain_runner_but_never_creates_entry_order() -> None:
    plan = plan_bybit_executed_entry_recovery(
        _record(planned_exit_mode="OPEN_ENDED_RUNNER"),
        order_truth=_truth(),
        positions=(_position(),),
    )

    assert plan.action is BybitExecutedEntryRecoveryAction.PROTECT
    assert plan.exit_mode == "OPEN_ENDED_RUNNER"
    assert isinstance(plan.protection_request, BybitDemoRunnerProtectionRequest)

    client = _RecoveryClient(plan.position)
    result = execute_bybit_executed_entry_recovery(plan, client=client)

    assert result.status is BybitExecutedEntryRecoveryStatus.PROTECTED
    assert client.events == ["protect-runner", "read-position"]
    assert client.order_requests == []


def test_adverse_fill_can_only_downgrade_to_protect_then_reduce_only_flatten() -> None:
    recovery = _record(planned_exit_mode="OPEN_ENDED_RUNNER", risk_budget="12")
    position = _position(average_price="120000")
    plan = plan_bybit_executed_entry_recovery(
        recovery,
        order_truth=_truth(),
        positions=(position,),
    )

    assert plan.action is BybitExecutedEntryRecoveryAction.PROTECT_THEN_FLATTEN
    assert plan.exit_mode == "FIXED_20_TARGET"
    client = _RecoveryClient(plan.position)
    result = execute_bybit_executed_entry_recovery(plan, client=client)

    assert result.status is BybitExecutedEntryRecoveryStatus.FLATTENED
    assert result.broker_position_closed is True
    assert len(client.order_requests) == 1
    assert client.order_requests[0].reduce_only is True
    assert client.events[-2:] == ["reduce-only-close", "read-position"]
    assert not any(event == "entry" for event in client.events)


def test_unverified_protection_fails_safe_to_reduce_only_flatten() -> None:
    plan = plan_bybit_executed_entry_recovery(
        _record(),
        order_truth=_truth(),
        positions=(_position(),),
    )
    client = _RecoveryClient(plan.position, verify_protection=False)

    result = execute_bybit_executed_entry_recovery(plan, client=client)

    assert result.status is BybitExecutedEntryRecoveryStatus.FLATTENED
    assert len(client.order_requests) == 1
    assert client.order_requests[0].reduce_only is True
    assert any(reason.startswith("RECOVERY_PROTECTION_STATE_UNVERIFIED") for reason in result.reasons)


def test_recovery_rejects_position_or_frozen_order_drift_before_mutation() -> None:
    recovery = _record()

    with pytest.raises(ValueError, match="position size does not match executed quantity"):
        plan_bybit_executed_entry_recovery(
            recovery,
            order_truth=_truth(),
            positions=(_position(size="0.005"),),
        )

    with pytest.raises(ValueError, match="broker side mismatch"):
        plan_bybit_executed_entry_recovery(
            recovery,
            order_truth=replace(_truth(), side="Sell"),
            positions=(_position(),),
        )
