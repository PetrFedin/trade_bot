from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from app.execution.bybit_demo import (
    BybitDemoOrderAck,
    BybitDemoOrderRequest,
    BybitDemoPosition,
    BybitDemoProtectionAck,
    BybitDemoProtectionRequest,
    BybitDemoRunnerProtectionAck,
    BybitDemoRunnerProtectionRequest,
)
from app.execution.bybit_demo_controller import (
    plan_bybit_demo_protection_after_fill,
    plan_bybit_demo_reduce_only_close,
    plan_bybit_demo_runner_protection_after_fill,
)
from app.execution.bybit_demo_excursion_tracker import (
    BybitDemoTradeExcursionState,
    start_bybit_demo_trade_excursion,
)
from app.execution.bybit_demo_protection_reconciliation import (
    BybitDemoProtectionReconciliationPolicy,
    reconcile_bybit_demo_emergency_flatten,
    reconcile_bybit_demo_exchange_protection,
)
from app.execution.bybit_entry_recovery import BybitEntryRecoveryRecord
from app.execution.bybit_order_lookup import BybitOrderTruth
from app.strategy.crypto_entry_economics import revalidate_entry_at_actual_taker_fee
from app.strategy.crypto_liquidation_safety import evaluate_crypto_liquidation_safety
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan
from app.strategy.crypto_runner_admission import evaluate_crypto_runner_admission

ProtectionRequest = BybitDemoProtectionRequest | BybitDemoRunnerProtectionRequest
ProtectionAck = BybitDemoProtectionAck | BybitDemoRunnerProtectionAck
_ZERO = Decimal("0")


class BybitExecutedEntryRecoveryAction(StrEnum):
    PROTECT = "PROTECT"
    PROTECT_THEN_FLATTEN = "PROTECT_THEN_FLATTEN"
    FLATTEN = "FLATTEN"


class BybitExecutedEntryRecoveryStatus(StrEnum):
    PROTECTED = "PROTECTED"
    FLATTENED = "FLATTENED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class BybitExecutedEntryRecoveryPlan:
    """Deterministic safety plan for an ENTRY that executed before local checkpoint creation."""

    entry_order_link_id: str
    broker_order_id: str
    recovery_record_sha256: str
    position: BybitDemoPosition
    post_fill_trade_plan: CryptoTradePlan
    exit_mode: str
    action: BybitExecutedEntryRecoveryAction
    protection_request: ProtectionRequest | None
    reasons: tuple[str, ...]
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitExecutedEntryRecoveryResult:
    status: BybitExecutedEntryRecoveryStatus
    reasons: tuple[str, ...]
    plan: BybitExecutedEntryRecoveryPlan
    protection_ack: ProtectionAck | None
    flatten_ack: BybitDemoOrderAck | None
    broker_position_closed: bool | None
    next_entry_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


class _RecoveryClient(Protocol):
    live_mainnet_order_routing_allowed: bool
    protection_state_read_supported: bool

    def set_full_position_protection(
        self,
        request: BybitDemoProtectionRequest,
    ) -> BybitDemoProtectionAck: ...

    def set_open_ended_position_protection(
        self,
        request: BybitDemoRunnerProtectionRequest,
    ) -> BybitDemoRunnerProtectionAck: ...

    def place_market_order(self, request: BybitDemoOrderRequest) -> BybitDemoOrderAck: ...

    def get_positions(self, *, settle_coin: str = "USDT") -> tuple[BybitDemoPosition, ...]: ...


def plan_bybit_executed_entry_recovery(
    recovery: BybitEntryRecoveryRecord,
    *,
    order_truth: BybitOrderTruth,
    positions: tuple[BybitDemoPosition, ...],
) -> BybitExecutedEntryRecoveryPlan:
    """Rebuild post-fill protection exclusively from frozen envelope + current broker truth.

    The function is pure: it does not mutate broker or PostgreSQL state. It rejects any mismatch
    instead of reading current strategy defaults. A pre-entry fixed target can never be upgraded
    to runner during restart recovery; a frozen runner may only remain a runner or downgrade.
    """

    if recovery.live_mainnet_order_routing_allowed:
        raise ValueError("executed-entry recovery rejected mainnet-capable recovery record")
    envelope = recovery.envelope
    envelope.validate()
    _validate_order_truth(envelope.entry_order_link_id, envelope.order_side, envelope.approved_order_quantity, envelope.trade_plan, order_truth)
    position = _single_matching_position(envelope.trade_plan, order_truth, positions)
    if position.average_price is None or not position.average_price.is_finite() or position.average_price <= 0:
        raise ValueError("executed-entry recovery requires positive broker average entry price")

    actual_fill_notional = position.average_price * position.size
    fee_decision = revalidate_entry_at_actual_taker_fee(
        envelope.trade_plan,
        execution_notional_usdt=actual_fill_notional,
        actual_taker_fee_rate=envelope.strategy_config.taker_fee_rate,
        strategy_config=envelope.strategy_config,
    )
    post_fill_plan = replace(
        envelope.trade_plan,
        notional_usdt=actual_fill_notional,
        reference_quantity=position.size,
        estimated_round_trip_cost_usdt=fee_decision.modeled_round_trip_cost_usdt,
        estimated_stop_loss_after_cost_usdt=fee_decision.modeled_stop_loss_after_cost_usdt,
        required_move_fraction=fee_decision.required_move_fraction,
        expected_net_edge_usd=fee_decision.modeled_expected_net_edge_usd,
    )
    post_fill_runner = evaluate_crypto_runner_admission(post_fill_plan)
    frozen_runner = envelope.planned_exit_mode == "OPEN_ENDED_RUNNER"
    runner_selected = frozen_runner and post_fill_runner.eligible and fee_decision.eligible
    exit_mode = "OPEN_ENDED_RUNNER" if runner_selected else "FIXED_20_TARGET"

    if runner_selected:
        protection_plan = plan_bybit_demo_runner_protection_after_fill(
            post_fill_plan,
            actual_average_entry_price=position.average_price,
            actual_filled_quantity=position.size,
            instrument=envelope.instrument,
            strategy_config=envelope.strategy_config,
        )
    else:
        protection_plan = plan_bybit_demo_protection_after_fill(
            post_fill_plan,
            actual_average_entry_price=position.average_price,
            actual_filled_quantity=position.size,
            instrument=envelope.instrument,
            strategy_config=envelope.strategy_config,
        )

    fee_reasons = tuple(f"POST_FILL_{reason}" for reason in fee_decision.reasons)
    runner_reasons = tuple(post_fill_runner.reasons)
    reasons = tuple(dict.fromkeys((*protection_plan.reasons, *fee_reasons, *runner_reasons)))
    protection = protection_plan.protection
    if protection is None:
        action = BybitExecutedEntryRecoveryAction.FLATTEN
    else:
        liquidation = evaluate_crypto_liquidation_safety(
            side=post_fill_plan.side,
            entry_price=position.average_price,
            hard_stop_price=protection.stop_loss_price,
            liquidation_price=position.liquidation_price,
        )
        if not liquidation.safe:
            reasons = tuple(dict.fromkeys((*reasons, liquidation.reason.value)))
        unsafe_after_fill = (
            protection_plan.flatten_required
            or not fee_decision.eligible
            or not liquidation.safe
        )
        action = (
            BybitExecutedEntryRecoveryAction.PROTECT_THEN_FLATTEN
            if unsafe_after_fill
            else BybitExecutedEntryRecoveryAction.PROTECT
        )

    if envelope.planned_exit_mode == "FIXED_20_TARGET" and exit_mode != "FIXED_20_TARGET":
        raise AssertionError("restart recovery upgraded a frozen fixed target")
    return BybitExecutedEntryRecoveryPlan(
        entry_order_link_id=envelope.entry_order_link_id,
        broker_order_id=order_truth.order_id,
        recovery_record_sha256=recovery.record_sha256,
        position=position,
        post_fill_trade_plan=post_fill_plan,
        exit_mode=exit_mode,
        action=action,
        protection_request=protection,
        reasons=reasons,
    )


def execute_bybit_executed_entry_recovery(
    plan: BybitExecutedEntryRecoveryPlan,
    *,
    client: _RecoveryClient,
    policy: BybitDemoProtectionReconciliationPolicy | None = None,
    sleeper: Any = None,
) -> BybitExecutedEntryRecoveryResult:
    """Execute only safety mutations: protection and, if needed, deterministic reduce-only close.

    This function has no code path that submits a risk-adding ENTRY. Successful protection still
    does not authorize new entry: the caller must durably create the active checkpoint and converge
    OMS state before normal product supervision resumes.
    """

    if plan.live_mainnet_order_routing_allowed:
        raise ValueError("executed-entry recovery plan unexpectedly permits mainnet routing")
    if client.live_mainnet_order_routing_allowed:
        raise ValueError("executed-entry recovery rejected mainnet-capable client")
    if not client.protection_state_read_supported:
        raise ValueError("executed-entry recovery requires protection-state reads")
    active_policy = BybitDemoProtectionReconciliationPolicy() if policy is None else policy
    active_policy.validate()
    sleep_fn = (lambda _delay: None) if sleeper is None else sleeper

    protection_ack: ProtectionAck | None = None
    protection_reason: str | None = None
    if plan.protection_request is not None:
        try:
            if isinstance(plan.protection_request, BybitDemoRunnerProtectionRequest):
                protection_ack = client.set_open_ended_position_protection(plan.protection_request)
            else:
                protection_ack = client.set_full_position_protection(plan.protection_request)
        except Exception as exc:  # noqa: BLE001 - recovery falls through to reduce-only flatten.
            protection_reason = f"RECOVERY_PROTECTION_WRITE_FAILED:{type(exc).__name__}"
        else:
            verification = reconcile_bybit_demo_exchange_protection(
                client=client,
                trade_plan=plan.post_fill_trade_plan,
                protection_ack=protection_ack,
                policy=active_policy,
                sleeper=sleep_fn,
            )
            if (
                plan.action is BybitExecutedEntryRecoveryAction.PROTECT
                and verification.reconciled
                and verification.position is not None
            ):
                return BybitExecutedEntryRecoveryResult(
                    status=BybitExecutedEntryRecoveryStatus.PROTECTED,
                    reasons=plan.reasons,
                    plan=replace(plan, position=verification.position),
                    protection_ack=protection_ack,
                    flatten_ack=None,
                    broker_position_closed=False,
                )
            if not verification.reconciled:
                protection_reason = (
                    f"RECOVERY_PROTECTION_STATE_UNVERIFIED:{verification.reason}"
                )

    return _flatten_recovered_position(
        plan,
        client=client,
        policy=active_policy,
        sleeper=sleep_fn,
        protection_ack=protection_ack,
        protection_reason=protection_reason,
    )


def build_recovered_entry_excursion_state(
    result: BybitExecutedEntryRecoveryResult,
) -> BybitDemoTradeExcursionState:
    if result.status is not BybitExecutedEntryRecoveryStatus.PROTECTED:
        raise ValueError("only a protected recovered entry can initialize active trade state")
    if result.broker_position_closed is not False:
        raise ValueError("protected recovered entry unexpectedly reports closed broker position")
    return start_bybit_demo_trade_excursion(
        result.plan.post_fill_trade_plan,
        position=result.plan.position,
    )


def _flatten_recovered_position(
    plan: BybitExecutedEntryRecoveryPlan,
    *,
    client: _RecoveryClient,
    policy: BybitDemoProtectionReconciliationPolicy,
    sleeper: Any,
    protection_ack: ProtectionAck | None,
    protection_reason: str | None,
) -> BybitExecutedEntryRecoveryResult:
    reasons = plan.reasons
    if protection_reason is not None:
        reasons = tuple(dict.fromkeys((*reasons, protection_reason)))
    try:
        close = plan_bybit_demo_reduce_only_close(
            plan.post_fill_trade_plan,
            open_quantity=plan.position.size,
            instrument=_instrument_from_plan(plan),
        )
        if not close.reduce_only:
            raise ValueError("executed-entry recovery close must be reduce-only")
        flatten_ack = client.place_market_order(close)
    except Exception as exc:  # noqa: BLE001 - unknown residual exposure remains explicit.
        return BybitExecutedEntryRecoveryResult(
            status=BybitExecutedEntryRecoveryStatus.UNRESOLVED,
            reasons=tuple(
                dict.fromkeys(
                    (*reasons, f"RECOVERY_REDUCE_ONLY_CLOSE_FAILED:{type(exc).__name__}")
                )
            ),
            plan=plan,
            protection_ack=protection_ack,
            flatten_ack=None,
            broker_position_closed=None,
        )

    flatten = reconcile_bybit_demo_emergency_flatten(
        client=client,
        trade_plan=plan.post_fill_trade_plan,
        policy=policy,
        sleeper=sleeper,
    )
    if flatten.position_closed:
        return BybitExecutedEntryRecoveryResult(
            status=BybitExecutedEntryRecoveryStatus.FLATTENED,
            reasons=reasons,
            plan=plan,
            protection_ack=protection_ack,
            flatten_ack=flatten_ack,
            broker_position_closed=True,
        )
    return BybitExecutedEntryRecoveryResult(
        status=BybitExecutedEntryRecoveryStatus.UNRESOLVED,
        reasons=tuple(dict.fromkeys((*reasons, flatten.reason))),
        plan=plan,
        protection_ack=protection_ack,
        flatten_ack=flatten_ack,
        broker_position_closed=False,
    )


def _instrument_from_plan(plan: BybitExecutedEntryRecoveryPlan):
    # The recovery hash binds the instrument used to build protection. The concrete instrument is
    # carried indirectly by the already-built protection request, so derive only close granularity
    # from the immutable plan is impossible. This guard is replaced by the recovery record owner.
    instrument = getattr(plan, "instrument", None)
    if instrument is None:
        raise ValueError("executed-entry recovery plan is missing immutable instrument")
    return instrument


def _validate_order_truth(
    entry_order_link_id: str,
    order_side: str,
    approved_quantity: Decimal,
    trade_plan: CryptoTradePlan,
    truth: BybitOrderTruth,
) -> None:
    expected_side = "Buy" if trade_plan.side is CryptoSide.LONG else "Sell"
    if truth.order_link_id != entry_order_link_id:
        raise ValueError("executed-entry recovery orderLinkId mismatch")
    if truth.symbol != trade_plan.symbol:
        raise ValueError("executed-entry recovery broker symbol mismatch")
    if truth.side != order_side or truth.side != expected_side:
        raise ValueError("executed-entry recovery broker side mismatch")
    if truth.quantity != approved_quantity:
        raise ValueError("executed-entry recovery broker quantity mismatch")
    if (
        not truth.cumulative_executed_quantity.is_finite()
        or truth.cumulative_executed_quantity <= _ZERO
        or truth.cumulative_executed_quantity > truth.quantity
    ):
        raise ValueError("executed-entry recovery requires positive bounded execution")
    if not truth.order_id.strip():
        raise ValueError("executed-entry recovery requires broker order id")


def _single_matching_position(
    trade_plan: CryptoTradePlan,
    truth: BybitOrderTruth,
    positions: tuple[BybitDemoPosition, ...],
) -> BybitDemoPosition:
    active = tuple(position for position in positions if position.size > _ZERO)
    if len(active) != 1:
        raise ValueError("executed-entry recovery requires exactly one active broker position")
    position = active[0]
    expected_side = "Buy" if trade_plan.side is CryptoSide.LONG else "Sell"
    if position.symbol != trade_plan.symbol or position.side != expected_side:
        raise ValueError("executed-entry recovery broker position identity mismatch")
    if not position.size.is_finite() or position.size <= _ZERO:
        raise ValueError("executed-entry recovery broker position size is invalid")
    if position.size != truth.cumulative_executed_quantity:
        raise ValueError("executed-entry recovery position size does not match executed quantity")
    return position
