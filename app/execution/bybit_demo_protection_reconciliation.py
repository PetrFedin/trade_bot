from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo import (
    BybitDemoProtectionAck,
    BybitDemoRunnerProtectionAck,
)
from app.execution.bybit_demo_controller import plan_bybit_demo_reduce_only_close
from app.execution.bybit_demo_cycle import BybitDemoCycleStatus
from app.execution.bybit_demo_orchestrator import (
    BybitDemoOrchestratorResult,
    execute_reconciled_guarded_bybit_demo_cycle,
)
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_liquidation_safety import evaluate_crypto_liquidation_safety
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


class BybitDemoProtectionStateReason(StrEnum):
    VERIFIED = "VERIFIED"
    POSITION_UNAVAILABLE = "POSITION_UNAVAILABLE"
    MULTIPLE_MATCHING_POSITIONS = "MULTIPLE_MATCHING_POSITIONS"
    STOP_LOSS_MISMATCH = "STOP_LOSS_MISMATCH"
    TAKE_PROFIT_MISMATCH = "TAKE_PROFIT_MISMATCH"
    UNEXPECTED_TAKE_PROFIT = "UNEXPECTED_TAKE_PROFIT"
    TRAILING_STOP_MISMATCH = "TRAILING_STOP_MISMATCH"
    UNEXPECTED_TRAILING_STOP = "UNEXPECTED_TRAILING_STOP"


@dataclass(frozen=True)
class BybitDemoProtectionReconciliationPolicy:
    attempts: int = 3
    delay_seconds: float = 0.10
    flatten_attempts: int = 4
    flatten_delay_seconds: float = 0.25

    def validate(self) -> None:
        if self.attempts < 1:
            raise ValueError("protection reconciliation attempts must be positive")
        if self.delay_seconds < 0:
            raise ValueError("protection reconciliation delay cannot be negative")
        if self.flatten_attempts < 1:
            raise ValueError("emergency flatten reconciliation attempts must be positive")
        if self.flatten_delay_seconds < 0:
            raise ValueError("emergency flatten reconciliation delay cannot be negative")


@dataclass(frozen=True)
class BybitDemoProtectionStateDecision:
    reconciled: bool
    reason: str
    position: BybitDemoProtectionPosition | None
    attempts_used: int
    runner_active_price_observable: bool = False
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoEmergencyFlattenDecision:
    position_closed: bool
    reason: str
    attempts_used: int
    residual_size: Decimal | None
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoProtectionReconciledOrchestratorResult(BybitDemoOrchestratorResult):
    protection_state_checked: bool = False
    protection_state_reconciled: bool = False
    protection_state_reason: str | None = None
    protection_reconciliation_attempts: int = 0
    runner_active_price_observable: bool = False
    emergency_flatten_requested: bool = False
    emergency_flatten_position_closed: bool | None = None
    emergency_flatten_reconciliation_attempts: int = 0
    emergency_flatten_residual_size: Decimal | None = None
    emergency_flatten_reconciliation_reason: str | None = None


Sleeper = Callable[[float], None]
BaseOrchestrator = Callable[..., BybitDemoOrchestratorResult]
ProtectionAck = BybitDemoProtectionAck | BybitDemoRunnerProtectionAck


def reconcile_bybit_demo_exchange_protection(
    *,
    client: Any,
    trade_plan: CryptoTradePlan,
    protection_ack: ProtectionAck,
    policy: BybitDemoProtectionReconciliationPolicy | None = None,
    sleeper: Sleeper = time.sleep,
) -> BybitDemoProtectionStateDecision:
    """Prove exchange-reported stop state after a trading-stop acknowledgement.

    Bybit position info exposes take-profit, stop-loss and trailing-stop distance. It does not
    expose the runner activation price, so runner verification proves hard stop + trailing
    distance + absence of an unintended fixed take-profit, while explicitly keeping activation
    price observability false.
    """

    active = BybitDemoProtectionReconciliationPolicy() if policy is None else policy
    active.validate()
    if not getattr(client, "protection_state_read_supported", False):
        raise ValueError("Bybit demo protection-state read capability is required")
    if getattr(client, "live_mainnet_order_routing_allowed", False):
        raise ValueError("protection reconciliation rejected a mainnet-capable client")

    last_reason = BybitDemoProtectionStateReason.POSITION_UNAVAILABLE.value
    last_position: BybitDemoProtectionPosition | None = None
    last_error_type: str | None = None
    for attempt in range(1, active.attempts + 1):
        try:
            positions = client.get_positions(settle_coin="USDT")
        except Exception as exc:  # noqa: BLE001 - read failures remain fail-closed after retries.
            last_error_type = type(exc).__name__
            if attempt < active.attempts and active.delay_seconds > 0:
                sleeper(active.delay_seconds)
            continue

        decision = evaluate_bybit_demo_exchange_protection(
            positions,
            trade_plan=trade_plan,
            protection_ack=protection_ack,
        )
        if decision.reconciled:
            return replace(decision, attempts_used=attempt)
        last_reason = decision.reason
        last_position = decision.position
        if attempt < active.attempts and active.delay_seconds > 0:
            sleeper(active.delay_seconds)

    if last_error_type is not None and last_position is None:
        last_reason = f"PROTECTION_STATE_READ_FAILED:{last_error_type}"
    return BybitDemoProtectionStateDecision(
        reconciled=False,
        reason=last_reason,
        position=last_position,
        attempts_used=active.attempts,
        runner_active_price_observable=False,
    )


def evaluate_bybit_demo_exchange_protection(
    positions: Sequence[BybitDemoProtectionPosition],
    *,
    trade_plan: CryptoTradePlan,
    protection_ack: ProtectionAck,
) -> BybitDemoProtectionStateDecision:
    expected_side = _position_side(trade_plan.side)
    matching = tuple(
        position
        for position in positions
        if position.symbol == trade_plan.symbol
        and position.side == expected_side
        and position.size > 0
    )
    if not matching:
        return _decision(False, BybitDemoProtectionStateReason.POSITION_UNAVAILABLE)
    if len(matching) != 1:
        return _decision(False, BybitDemoProtectionStateReason.MULTIPLE_MATCHING_POSITIONS)
    position = matching[0]

    if position.stop_loss_price != protection_ack.stop_loss_price:
        return _decision(
            False,
            BybitDemoProtectionStateReason.STOP_LOSS_MISMATCH,
            position,
        )

    if isinstance(protection_ack, BybitDemoProtectionAck):
        if position.take_profit_price != protection_ack.take_profit_price:
            return _decision(
                False,
                BybitDemoProtectionStateReason.TAKE_PROFIT_MISMATCH,
                position,
            )
        if position.trailing_stop_distance is not None:
            return _decision(
                False,
                BybitDemoProtectionStateReason.UNEXPECTED_TRAILING_STOP,
                position,
            )
    else:
        if position.take_profit_price is not None:
            return _decision(
                False,
                BybitDemoProtectionStateReason.UNEXPECTED_TAKE_PROFIT,
                position,
            )
        if position.trailing_stop_distance != protection_ack.trailing_stop_distance:
            return _decision(
                False,
                BybitDemoProtectionStateReason.TRAILING_STOP_MISMATCH,
                position,
            )

    return _decision(True, BybitDemoProtectionStateReason.VERIFIED, position)


def reconcile_bybit_demo_emergency_flatten(
    *,
    client: Any,
    trade_plan: CryptoTradePlan,
    policy: BybitDemoProtectionReconciliationPolicy | None = None,
    sleeper: Sleeper = time.sleep,
) -> BybitDemoEmergencyFlattenDecision:
    """Confirm a reduce-only emergency close from current position state, not order ACK."""

    active = BybitDemoProtectionReconciliationPolicy() if policy is None else policy
    active.validate()
    if not getattr(client, "protection_state_read_supported", False):
        raise ValueError("emergency flatten reconciliation requires position-state reads")
    if getattr(client, "live_mainnet_order_routing_allowed", False):
        raise ValueError("emergency flatten reconciliation rejected mainnet-capable client")

    expected_side = _position_side(trade_plan.side)
    residual_size: Decimal | None = None
    successful_read = False
    last_error_type: str | None = None
    for attempt in range(1, active.flatten_attempts + 1):
        try:
            positions = client.get_positions(settle_coin="USDT")
        except Exception as exc:  # noqa: BLE001 - unresolved close state must remain fail-closed.
            last_error_type = type(exc).__name__
            if attempt < active.flatten_attempts and active.flatten_delay_seconds > 0:
                sleeper(active.flatten_delay_seconds)
            continue

        successful_read = True
        matching = tuple(
            position
            for position in positions
            if position.symbol == trade_plan.symbol
            and position.side == expected_side
            and position.size > 0
        )
        if not matching:
            return BybitDemoEmergencyFlattenDecision(
                position_closed=True,
                reason="EMERGENCY_FLATTEN_CONFIRMED_CLOSED",
                attempts_used=attempt,
                residual_size=Decimal("0"),
            )
        residual_size = sum(
            (position.size for position in matching),
            start=Decimal("0"),
        )
        if attempt < active.flatten_attempts and active.flatten_delay_seconds > 0:
            sleeper(active.flatten_delay_seconds)

    if not successful_read and last_error_type is not None:
        reason = f"EMERGENCY_FLATTEN_POSITION_READ_FAILED:{last_error_type}"
    else:
        reason = "EMERGENCY_FLATTEN_RESIDUAL_POSITION"
    return BybitDemoEmergencyFlattenDecision(
        position_closed=False,
        reason=reason,
        attempts_used=active.flatten_attempts,
        residual_size=residual_size,
    )


def execute_protection_reconciled_guarded_bybit_demo_cycle(
    trade_plan: CryptoTradePlan,
    *,
    instrument: BybitInstrumentSpec,
    client: Any,
    protection_reconciliation_policy: BybitDemoProtectionReconciliationPolicy | None = None,
    protection_sleeper: Sleeper = time.sleep,
    base_orchestrator: BaseOrchestrator = execute_reconciled_guarded_bybit_demo_cycle,
    **orchestrator_kwargs: Any,
) -> BybitDemoProtectionReconciledOrchestratorResult:
    """Run the existing orchestrator, then prove protection before accepting PROTECTED state."""

    active_policy = (
        BybitDemoProtectionReconciliationPolicy()
        if protection_reconciliation_policy is None
        else protection_reconciliation_policy
    )
    active_policy.validate()
    if not getattr(client, "protection_state_read_supported", False):
        raise ValueError("canonical demo writes require protection-state read capability")
    if getattr(client, "live_mainnet_order_routing_allowed", False):
        raise ValueError("protection-reconciled orchestrator rejected mainnet-capable client")

    base = base_orchestrator(
        trade_plan,
        instrument=instrument,
        client=client,
        **orchestrator_kwargs,
    )
    if base.live_mainnet_order_routing_allowed:
        raise ValueError("base demo orchestrator returned live mainnet permission")
    cycle = base.cycle_result
    if cycle is None or cycle.status is not BybitDemoCycleStatus.PROTECTED:
        return _wrap(base)
    if cycle.protection_ack is None or cycle.reconciled_position is None:
        raise ValueError("protected demo cycle is missing protection or position evidence")

    verification = reconcile_bybit_demo_exchange_protection(
        client=client,
        trade_plan=trade_plan,
        protection_ack=cycle.protection_ack,
        policy=active_policy,
        sleeper=protection_sleeper,
    )
    if not verification.reconciled or verification.position is None:
        reason = f"EXCHANGE_PROTECTION_STATE_UNVERIFIED:{verification.reason}"
        open_quantity = (
            cycle.reconciled_position.size
            if verification.position is None
            else verification.position.size
        )
        return _flatten_untrusted_position(
            base,
            trade_plan=trade_plan,
            instrument=instrument,
            client=client,
            verification=verification,
            reason=reason,
            open_quantity=open_quantity,
            policy=active_policy,
            sleeper=protection_sleeper,
        )

    fresh_position = verification.position
    if fresh_position.average_price is None:
        return _flatten_untrusted_position(
            base,
            trade_plan=trade_plan,
            instrument=instrument,
            client=client,
            verification=verification,
            reason="POST_PROTECTION_ENTRY_PRICE_UNAVAILABLE",
            open_quantity=fresh_position.size,
            policy=active_policy,
            sleeper=protection_sleeper,
        )
    liquidation = evaluate_crypto_liquidation_safety(
        side=trade_plan.side,
        entry_price=fresh_position.average_price,
        hard_stop_price=cycle.protection_ack.stop_loss_price,
        liquidation_price=fresh_position.liquidation_price,
    )
    if not liquidation.safe:
        return _flatten_untrusted_position(
            base,
            trade_plan=trade_plan,
            instrument=instrument,
            client=client,
            verification=verification,
            reason=f"POST_PROTECTION_{liquidation.reason.value}",
            open_quantity=fresh_position.size,
            policy=active_policy,
            sleeper=protection_sleeper,
        )

    verified_cycle = replace(
        cycle,
        reconciled_position=fresh_position,
        liquidation_safety_reason=liquidation.reason.value,
        stop_to_liquidation_r=liquidation.stop_to_liquidation_r,
    )
    return _wrap(
        replace(base, cycle_result=verified_cycle),
        verification=verification,
    )


def _flatten_untrusted_position(
    base: BybitDemoOrchestratorResult,
    *,
    trade_plan: CryptoTradePlan,
    instrument: BybitInstrumentSpec,
    client: Any,
    verification: BybitDemoProtectionStateDecision,
    reason: str,
    open_quantity: Decimal,
    policy: BybitDemoProtectionReconciliationPolicy,
    sleeper: Sleeper,
) -> BybitDemoProtectionReconciledOrchestratorResult:
    cycle = base.cycle_result
    if cycle is None:
        raise ValueError("protection flatten requires a cycle result")
    try:
        close = plan_bybit_demo_reduce_only_close(
            trade_plan,
            open_quantity=open_quantity,
            instrument=instrument,
        )
        flatten_ack = client.place_market_order(close)
    except Exception as exc:  # noqa: BLE001 - unresolved protection requires explicit close status.
        failed_cycle = replace(
            cycle,
            status=BybitDemoCycleStatus.FLATTEN_REQUEST_FAILED,
            reasons=(reason, f"EMERGENCY_REDUCE_ONLY_CLOSE_FAILED:{type(exc).__name__}"),
            next_entry_allowed=False,
        )
        return _wrap(
            replace(
                base,
                reasons=failed_cycle.reasons,
                cycle_result=failed_cycle,
                next_entry_allowed=False,
            ),
            verification=verification,
        )

    flatten = reconcile_bybit_demo_emergency_flatten(
        client=client,
        trade_plan=trade_plan,
        policy=policy,
        sleeper=sleeper,
    )
    reasons = (reason,)
    if not flatten.position_closed:
        reasons = (
            reason,
            f"EMERGENCY_FLATTEN_UNCONFIRMED:{flatten.reason}",
        )
    flattened_cycle = replace(
        cycle,
        status=BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED,
        reasons=reasons,
        flatten_ack=flatten_ack,
        next_entry_allowed=False,
    )
    return _wrap(
        replace(
            base,
            reasons=flattened_cycle.reasons,
            cycle_result=flattened_cycle,
            next_entry_allowed=False,
        ),
        verification=verification,
        emergency_flatten_requested=True,
        flatten=flatten,
    )


def _position_side(side: CryptoSide) -> str:
    return "Buy" if side is CryptoSide.LONG else "Sell"


def _decision(
    reconciled: bool,
    reason: BybitDemoProtectionStateReason,
    position: BybitDemoProtectionPosition | None = None,
) -> BybitDemoProtectionStateDecision:
    return BybitDemoProtectionStateDecision(
        reconciled=reconciled,
        reason=reason.value,
        position=position,
        attempts_used=1,
        runner_active_price_observable=False,
    )


def _wrap(
    base: BybitDemoOrchestratorResult,
    *,
    verification: BybitDemoProtectionStateDecision | None = None,
    emergency_flatten_requested: bool = False,
    flatten: BybitDemoEmergencyFlattenDecision | None = None,
) -> BybitDemoProtectionReconciledOrchestratorResult:
    return BybitDemoProtectionReconciledOrchestratorResult(
        status=base.status,
        reasons=base.reasons,
        cycle_result=base.cycle_result,
        previous_trade_gate_checked=base.previous_trade_gate_checked,
        next_entry_allowed=base.next_entry_allowed,
        demo_only=base.demo_only,
        strategy_promotion_allowed=base.strategy_promotion_allowed,
        live_mainnet_order_routing_allowed=False,
        previous_trade_accounting=base.previous_trade_accounting,
        protection_state_checked=verification is not None,
        protection_state_reconciled=(
            False if verification is None else verification.reconciled
        ),
        protection_state_reason=None if verification is None else verification.reason,
        protection_reconciliation_attempts=(
            0 if verification is None else verification.attempts_used
        ),
        runner_active_price_observable=(
            False if verification is None else verification.runner_active_price_observable
        ),
        emergency_flatten_requested=emergency_flatten_requested,
        emergency_flatten_position_closed=(
            None if flatten is None else flatten.position_closed
        ),
        emergency_flatten_reconciliation_attempts=(
            0 if flatten is None else flatten.attempts_used
        ),
        emergency_flatten_residual_size=(
            None if flatten is None else flatten.residual_size
        ),
        emergency_flatten_reconciliation_reason=(
            None if flatten is None else flatten.reason
        ),
    )
