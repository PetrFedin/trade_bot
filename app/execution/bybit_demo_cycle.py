from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.execution.bybit_demo import (
    BybitDemoFeeRate,
    BybitDemoOrderAck,
    BybitDemoOrderClient,
    BybitDemoPosition,
    BybitDemoProtectionAck,
    BybitDemoRunnerProtectionAck,
)
from app.execution.bybit_demo_controller import (
    plan_bybit_demo_entry,
    plan_bybit_demo_protection_after_fill,
    plan_bybit_demo_reduce_only_close,
    plan_bybit_demo_runner_protection_after_fill,
)
from app.execution.bybit_entry_recovery import (
    BybitEntryRecoveryEnvelope,
    BybitEntryRecoveryReceipt,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_entry_economics import revalidate_entry_at_actual_taker_fee
from app.strategy.crypto_liquidation_safety import evaluate_crypto_liquidation_safety
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan
from app.strategy.crypto_runner_admission import evaluate_crypto_runner_admission
from app.strategy.crypto_session_risk import CryptoSessionRiskPolicy, CryptoSessionRiskState


class BybitDemoCycleStatus(StrEnum):
    DEMO_WRITES_DISABLED = "DEMO_WRITES_DISABLED"
    PREEXISTING_POSITION_BLOCKED = "PREEXISTING_POSITION_BLOCKED"
    ENTRY_BLOCKED = "ENTRY_BLOCKED"
    ENTRY_ACKED_FILL_UNRESOLVED = "ENTRY_ACKED_FILL_UNRESOLVED"
    PROTECTED = "PROTECTED"
    PROTECTED_THEN_FLATTEN_REQUESTED = "PROTECTED_THEN_FLATTEN_REQUESTED"
    PROTECTION_FAILED_FLATTEN_REQUESTED = "PROTECTION_FAILED_FLATTEN_REQUESTED"
    FLATTEN_REQUEST_FAILED = "FLATTEN_REQUEST_FAILED"


@dataclass(frozen=True)
class BybitDemoCyclePolicy:
    writes_enabled: bool = False
    reconciliation_attempts: int = 4
    reconciliation_delay_seconds: float = 0.25
    require_entry_recovery_envelope: bool = False

    def validate(self) -> None:
        if self.reconciliation_attempts < 1:
            raise ValueError("Bybit demo reconciliation attempts must be positive")
        if self.reconciliation_delay_seconds < 0:
            raise ValueError("Bybit demo reconciliation delay cannot be negative")
        if not isinstance(self.require_entry_recovery_envelope, bool):
            raise ValueError("entry recovery envelope requirement must be boolean")


ProtectionAck = BybitDemoProtectionAck | BybitDemoRunnerProtectionAck


@dataclass(frozen=True)
class BybitDemoCycleResult:
    status: BybitDemoCycleStatus
    reasons: tuple[str, ...]
    entry_ack: BybitDemoOrderAck | None
    protection_ack: ProtectionAck | None
    flatten_ack: BybitDemoOrderAck | None
    reconciled_position: BybitDemoPosition | None
    next_entry_allowed: bool
    demo_order_writes_enabled: bool
    account_taker_fee_rate: Decimal | None = None
    account_maker_fee_rate: Decimal | None = None
    exit_mode: str | None = None
    runner_admission_reasons: tuple[str, ...] = ()
    liquidation_safety_reason: str | None = None
    stop_to_liquidation_r: Decimal | None = None
    live_mainnet_order_routing_allowed: bool = False


class _DemoClient(Protocol):
    @property
    def live_mainnet_order_routing_allowed(self) -> bool: ...

    def get_fee_rate(self, *, symbol: str) -> BybitDemoFeeRate: ...

    def get_positions(self, *, settle_coin: str = "USDT") -> tuple[BybitDemoPosition, ...]: ...

    def place_market_order(self, request: object) -> BybitDemoOrderAck: ...

    def set_full_position_protection(self, request: object) -> BybitDemoProtectionAck: ...

    def set_open_ended_position_protection(
        self,
        request: object,
    ) -> BybitDemoRunnerProtectionAck: ...


class _EntryRecoveryStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool
    immutable_records: bool

    def persist(self, envelope: BybitEntryRecoveryEnvelope) -> BybitEntryRecoveryReceipt: ...


Sleeper = Callable[[float], None]


def execute_bybit_demo_trade_cycle(
    trade_plan: CryptoTradePlan,
    *,
    instrument: BybitInstrumentSpec,
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    client: BybitDemoOrderClient | _DemoClient,
    cycle_policy: BybitDemoCyclePolicy | None = None,
    session_policy: CryptoSessionRiskPolicy | None = None,
    entry_recovery_store: _EntryRecoveryStore | None = None,
    sleeper: Sleeper = time.sleep,
) -> BybitDemoCycleResult:
    """Execute one fail-closed demo entry -> fill reconcile -> conditional exit-protection cycle.

    The same excess-edge rule used by historical research is applied here: a normal accepted
    >=$20 trade keeps a fixed, cost-aware $20 take-profit unless its expected net edge clears
    the runner gate. The runner gate is checked with the account fee tier and instrument-sized
    quantity before entry, then rechecked using the actual reconciled fill. A favorable fill may
    not upgrade a pre-entry fixed target into a runner; an adverse fill can only downgrade the
    runner or force a protected reduce-only flatten. After exchange-native protection is
    acknowledged, liquidation-price safety may veto the position but can never authorize entry.

    Canonical OMS clients require an immutable restart-recovery envelope. The exact fee-adjusted
    trade plan, instrument contract and effective strategy configuration are persisted before the
    ENTRY POST. A missing/unavailable store blocks the broker mutation rather than reconstructing
    protection parameters from guesses after a crash.
    """

    policy = BybitDemoCyclePolicy() if cycle_policy is None else cycle_policy
    policy.validate()
    if client.live_mainnet_order_routing_allowed:
        raise ValueError("Bybit demo cycle rejected a client that permits mainnet routing")
    active_recovery_store = (
        entry_recovery_store
        if entry_recovery_store is not None
        else getattr(client, "entry_recovery_store", None)
    )
    recovery_required = policy.require_entry_recovery_envelope or bool(
        getattr(client, "entry_recovery_required", False)
    )
    if not policy.writes_enabled:
        return _result(
            BybitDemoCycleStatus.DEMO_WRITES_DISABLED,
            reasons=("EXPLICIT_DEMO_WRITE_ENABLE_REQUIRED",),
            writes_enabled=False,
        )

    preexisting = _matching_open_position(client.get_positions(), trade_plan)
    if preexisting is not None:
        return _result(
            BybitDemoCycleStatus.PREEXISTING_POSITION_BLOCKED,
            reasons=("PREEXISTING_SYMBOL_POSITION_REQUIRES_RECONCILIATION",),
            position=preexisting,
            writes_enabled=True,
        )

    entry_plan = plan_bybit_demo_entry(
        trade_plan,
        instrument=instrument,
        session_state=session_state,
        session_policy=session_policy,
    )
    if not entry_plan.eligible or entry_plan.order is None:
        return _result(
            BybitDemoCycleStatus.ENTRY_BLOCKED,
            reasons=entry_plan.reasons,
            writes_enabled=True,
        )

    try:
        fee_rate = client.get_fee_rate(symbol=trade_plan.symbol)
    except Exception as exc:  # noqa: BLE001 - unresolved fees must block before any order write.
        return _result(
            BybitDemoCycleStatus.ENTRY_BLOCKED,
            reasons=(f"ACCOUNT_FEE_RATE_RECONCILIATION_FAILED:{type(exc).__name__}",),
            writes_enabled=True,
        )

    effective_strategy_config = replace(
        strategy_config,
        taker_fee_rate=fee_rate.taker_fee_rate,
    )
    execution_notional = entry_plan.order.quantity * trade_plan.reference_price
    fee_decision = revalidate_entry_at_actual_taker_fee(
        trade_plan,
        execution_notional_usdt=execution_notional,
        actual_taker_fee_rate=fee_rate.taker_fee_rate,
        strategy_config=effective_strategy_config,
    )
    if not fee_decision.eligible:
        return _result(
            BybitDemoCycleStatus.ENTRY_BLOCKED,
            reasons=fee_decision.reasons,
            fee_rate=fee_rate,
            writes_enabled=True,
        )

    fee_adjusted_plan = replace(
        trade_plan,
        notional_usdt=execution_notional,
        reference_quantity=entry_plan.order.quantity,
        estimated_round_trip_cost_usdt=fee_decision.modeled_round_trip_cost_usdt,
        estimated_stop_loss_after_cost_usdt=fee_decision.modeled_stop_loss_after_cost_usdt,
        required_move_fraction=fee_decision.required_move_fraction,
        expected_net_edge_usd=fee_decision.modeled_expected_net_edge_usd,
    )
    pre_fill_runner_admission = evaluate_crypto_runner_admission(fee_adjusted_plan)
    planned_exit_mode = (
        "OPEN_ENDED_RUNNER" if pre_fill_runner_admission.eligible else "FIXED_20_TARGET"
    )

    recovery_block = _persist_entry_recovery_envelope(
        entry_plan.order.order_link_id,
        order_side=entry_plan.order.side,
        approved_order_quantity=entry_plan.order.quantity,
        trade_plan=fee_adjusted_plan,
        instrument=instrument,
        strategy_config=effective_strategy_config,
        planned_exit_mode=planned_exit_mode,
        store=active_recovery_store,
        required=recovery_required,
    )
    if recovery_block is not None:
        return _result(
            BybitDemoCycleStatus.ENTRY_BLOCKED,
            reasons=(recovery_block,),
            fee_rate=fee_rate,
            exit_mode=planned_exit_mode,
            runner_admission_reasons=pre_fill_runner_admission.reasons,
            writes_enabled=True,
        )

    entry_ack = client.place_market_order(entry_plan.order)
    position = _reconcile_position(
        client,
        trade_plan=trade_plan,
        attempts=policy.reconciliation_attempts,
        delay_seconds=policy.reconciliation_delay_seconds,
        sleeper=sleeper,
    )
    if position is None or position.average_price is None:
        return _result(
            BybitDemoCycleStatus.ENTRY_ACKED_FILL_UNRESOLVED,
            reasons=("ORDER_ACK_IS_NOT_FILL_CONFIRMATION",),
            entry_ack=entry_ack,
            fee_rate=fee_rate,
            exit_mode=planned_exit_mode,
            runner_admission_reasons=pre_fill_runner_admission.reasons,
            writes_enabled=True,
        )

    actual_fill_notional = position.average_price * position.size
    post_fill_fee_decision = revalidate_entry_at_actual_taker_fee(
        fee_adjusted_plan,
        execution_notional_usdt=actual_fill_notional,
        actual_taker_fee_rate=fee_rate.taker_fee_rate,
        strategy_config=effective_strategy_config,
    )
    post_fill_plan = replace(
        fee_adjusted_plan,
        notional_usdt=actual_fill_notional,
        reference_quantity=position.size,
        estimated_round_trip_cost_usdt=post_fill_fee_decision.modeled_round_trip_cost_usdt,
        estimated_stop_loss_after_cost_usdt=(
            post_fill_fee_decision.modeled_stop_loss_after_cost_usdt
        ),
        required_move_fraction=post_fill_fee_decision.required_move_fraction,
        expected_net_edge_usd=post_fill_fee_decision.modeled_expected_net_edge_usd,
    )
    post_fill_runner_admission = evaluate_crypto_runner_admission(post_fill_plan)
    runner_selected = (
        pre_fill_runner_admission.eligible
        and post_fill_runner_admission.eligible
        and post_fill_fee_decision.eligible
    )
    exit_mode = "OPEN_ENDED_RUNNER" if runner_selected else "FIXED_20_TARGET"
    runner_admission_reasons = tuple(
        dict.fromkeys(
            (*pre_fill_runner_admission.reasons, *post_fill_runner_admission.reasons)
        )
    )
    post_fill_invalid_reasons = tuple(
        f"POST_FILL_{reason}" for reason in post_fill_fee_decision.reasons
    )

    if runner_selected:
        protection_plan = plan_bybit_demo_runner_protection_after_fill(
            post_fill_plan,
            actual_average_entry_price=position.average_price,
            actual_filled_quantity=position.size,
            instrument=instrument,
            strategy_config=effective_strategy_config,
        )
    else:
        protection_plan = plan_bybit_demo_protection_after_fill(
            post_fill_plan,
            actual_average_entry_price=position.average_price,
            actual_filled_quantity=position.size,
            instrument=instrument,
            strategy_config=effective_strategy_config,
        )

    if protection_plan.protection is None:
        return _flatten_after_protection_failure(
            trade_plan,
            instrument=instrument,
            client=client,
            position=position,
            entry_ack=entry_ack,
            fee_rate=fee_rate,
            exit_mode=exit_mode,
            runner_admission_reasons=runner_admission_reasons,
            reasons=tuple(dict.fromkeys((*protection_plan.reasons, *post_fill_invalid_reasons))),
        )

    try:
        if runner_selected:
            protection_ack: ProtectionAck = client.set_open_ended_position_protection(
                protection_plan.protection
            )
        else:
            protection_ack = client.set_full_position_protection(protection_plan.protection)
    except Exception as exc:  # noqa: BLE001 - protection failure must trigger a close attempt.
        return _flatten_after_protection_failure(
            trade_plan,
            instrument=instrument,
            client=client,
            position=position,
            entry_ack=entry_ack,
            fee_rate=fee_rate,
            exit_mode=exit_mode,
            runner_admission_reasons=runner_admission_reasons,
            reasons=(f"EXCHANGE_PROTECTION_WRITE_FAILED:{type(exc).__name__}",),
        )

    flatten_reasons = tuple(
        dict.fromkeys((*protection_plan.reasons, *post_fill_invalid_reasons))
    )
    if protection_plan.flatten_required or not post_fill_fee_decision.eligible:
        return _flatten_protected_position(
            trade_plan,
            instrument=instrument,
            client=client,
            position=position,
            entry_ack=entry_ack,
            protection_ack=protection_ack,
            fee_rate=fee_rate,
            exit_mode=exit_mode,
            runner_admission_reasons=runner_admission_reasons,
            reasons=flatten_reasons,
        )

    liquidation = evaluate_crypto_liquidation_safety(
        side=trade_plan.side,
        entry_price=position.average_price,
        hard_stop_price=protection_plan.protection.stop_loss_price,
        liquidation_price=position.liquidation_price,
    )
    if not liquidation.safe:
        return _flatten_protected_position(
            trade_plan,
            instrument=instrument,
            client=client,
            position=position,
            entry_ack=entry_ack,
            protection_ack=protection_ack,
            fee_rate=fee_rate,
            exit_mode=exit_mode,
            runner_admission_reasons=runner_admission_reasons,
            reasons=(liquidation.reason.value,),
            liquidation_safety_reason=liquidation.reason.value,
            stop_to_liquidation_r=liquidation.stop_to_liquidation_r,
        )

    return _result(
        BybitDemoCycleStatus.PROTECTED,
        reasons=(),
        entry_ack=entry_ack,
        protection_ack=protection_ack,
        position=position,
        next_entry_allowed=True,
        fee_rate=fee_rate,
        exit_mode=exit_mode,
        runner_admission_reasons=runner_admission_reasons,
        liquidation_safety_reason=liquidation.reason.value,
        stop_to_liquidation_r=liquidation.stop_to_liquidation_r,
        writes_enabled=True,
    )


def _persist_entry_recovery_envelope(
    entry_order_link_id: str,
    *,
    order_side: str,
    approved_order_quantity: Decimal,
    trade_plan: CryptoTradePlan,
    instrument: BybitInstrumentSpec,
    strategy_config: CryptoPerpStrategyConfig,
    planned_exit_mode: str,
    store: _EntryRecoveryStore | None,
    required: bool,
) -> str | None:
    if store is None:
        return "ENTRY_RECOVERY_ENVELOPE_STORE_REQUIRED" if required else None
    if getattr(store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("entry recovery store unexpectedly permits mainnet routing")
    if getattr(store, "order_writes_supported", True) is not False:
        raise ValueError("entry recovery store must not expose broker order writes")
    if getattr(store, "immutable_records", False) is not True:
        raise ValueError("entry recovery store must preserve immutable records")
    envelope = BybitEntryRecoveryEnvelope(
        entry_order_link_id=entry_order_link_id,
        order_side=order_side,
        approved_order_quantity=approved_order_quantity,
        trade_plan=trade_plan,
        instrument=instrument,
        strategy_config=strategy_config,
        planned_exit_mode=planned_exit_mode,
    )
    try:
        receipt = store.persist(envelope)
    except Exception as exc:  # noqa: BLE001 - recovery durability failure must block before POST.
        return f"ENTRY_RECOVERY_ENVELOPE_PERSIST_FAILED:{type(exc).__name__}"
    if receipt.live_mainnet_order_routing_allowed:
        raise ValueError("entry recovery receipt unexpectedly permits mainnet routing")
    if receipt.entry_order_link_id != entry_order_link_id:
        raise ValueError("entry recovery receipt orderLinkId mismatch")
    return None


def _reconcile_position(
    client: _DemoClient,
    *,
    trade_plan: CryptoTradePlan,
    attempts: int,
    delay_seconds: float,
    sleeper: Sleeper,
) -> BybitDemoPosition | None:
    for attempt in range(attempts):
        position = _matching_open_position(client.get_positions(), trade_plan)
        if position is not None and position.average_price is not None:
            return position
        if attempt + 1 < attempts and delay_seconds > 0:
            sleeper(delay_seconds)
    return None


def _matching_open_position(
    positions: tuple[BybitDemoPosition, ...],
    trade_plan: CryptoTradePlan,
) -> BybitDemoPosition | None:
    expected_side = "Buy" if trade_plan.side is CryptoSide.LONG else "Sell"
    for position in positions:
        if position.symbol != trade_plan.symbol or position.size <= 0:
            continue
        if position.side != expected_side:
            return position
        return position
    return None


def _flatten_after_protection_failure(
    trade_plan: CryptoTradePlan,
    *,
    instrument: BybitInstrumentSpec,
    client: _DemoClient,
    position: BybitDemoPosition,
    entry_ack: BybitDemoOrderAck,
    fee_rate: BybitDemoFeeRate,
    exit_mode: str,
    runner_admission_reasons: tuple[str, ...],
    reasons: tuple[str, ...],
) -> BybitDemoCycleResult:
    try:
        close = plan_bybit_demo_reduce_only_close(
            trade_plan,
            open_quantity=position.size,
            instrument=instrument,
        )
        flatten_ack = client.place_market_order(close)
    except Exception as exc:  # noqa: BLE001 - result must explicitly expose failed emergency close.
        return _result(
            BybitDemoCycleStatus.FLATTEN_REQUEST_FAILED,
            reasons=(*reasons, f"EMERGENCY_REDUCE_ONLY_CLOSE_FAILED:{type(exc).__name__}"),
            entry_ack=entry_ack,
            position=position,
            fee_rate=fee_rate,
            exit_mode=exit_mode,
            runner_admission_reasons=runner_admission_reasons,
            writes_enabled=True,
        )
    return _result(
        BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED,
        reasons=reasons,
        entry_ack=entry_ack,
        flatten_ack=flatten_ack,
        position=position,
        fee_rate=fee_rate,
        exit_mode=exit_mode,
        runner_admission_reasons=runner_admission_reasons,
        writes_enabled=True,
    )


def _flatten_protected_position(
    trade_plan: CryptoTradePlan,
    *,
    instrument: BybitInstrumentSpec,
    client: _DemoClient,
    position: BybitDemoPosition,
    entry_ack: BybitDemoOrderAck,
    protection_ack: ProtectionAck,
    fee_rate: BybitDemoFeeRate,
    exit_mode: str,
    runner_admission_reasons: tuple[str, ...],
    reasons: tuple[str, ...],
    liquidation_safety_reason: str | None = None,
    stop_to_liquidation_r: Decimal | None = None,
) -> BybitDemoCycleResult:
    try:
        close = plan_bybit_demo_reduce_only_close(
            trade_plan,
            open_quantity=position.size,
            instrument=instrument,
        )
        flatten_ack = client.place_market_order(close)
    except Exception as exc:  # noqa: BLE001 - result must explicitly expose failed emergency close.
        return _result(
            BybitDemoCycleStatus.FLATTEN_REQUEST_FAILED,
            reasons=(*reasons, f"EMERGENCY_REDUCE_ONLY_CLOSE_FAILED:{type(exc).__name__}"),
            entry_ack=entry_ack,
            protection_ack=protection_ack,
            position=position,
            fee_rate=fee_rate,
            exit_mode=exit_mode,
            runner_admission_reasons=runner_admission_reasons,
            liquidation_safety_reason=liquidation_safety_reason,
            stop_to_liquidation_r=stop_to_liquidation_r,
            writes_enabled=True,
        )
    return _result(
        BybitDemoCycleStatus.PROTECTED_THEN_FLATTEN_REQUESTED,
        reasons=reasons,
        entry_ack=entry_ack,
        protection_ack=protection_ack,
        flatten_ack=flatten_ack,
        position=position,
        fee_rate=fee_rate,
        exit_mode=exit_mode,
        runner_admission_reasons=runner_admission_reasons,
        liquidation_safety_reason=liquidation_safety_reason,
        stop_to_liquidation_r=stop_to_liquidation_r,
        writes_enabled=True,
    )


def _result(
    status: BybitDemoCycleStatus,
    *,
    reasons: tuple[str, ...],
    entry_ack: BybitDemoOrderAck | None = None,
    protection_ack: ProtectionAck | None = None,
    flatten_ack: BybitDemoOrderAck | None = None,
    position: BybitDemoPosition | None = None,
    next_entry_allowed: bool = False,
    fee_rate: BybitDemoFeeRate | None = None,
    exit_mode: str | None = None,
    runner_admission_reasons: tuple[str, ...] = (),
    liquidation_safety_reason: str | None = None,
    stop_to_liquidation_r: Decimal | None = None,
    writes_enabled: bool,
) -> BybitDemoCycleResult:
    return BybitDemoCycleResult(
        status=status,
        reasons=reasons,
        entry_ack=entry_ack,
        protection_ack=protection_ack,
        flatten_ack=flatten_ack,
        reconciled_position=position,
        next_entry_allowed=next_entry_allowed,
        demo_order_writes_enabled=writes_enabled,
        account_taker_fee_rate=None if fee_rate is None else fee_rate.taker_fee_rate,
        account_maker_fee_rate=None if fee_rate is None else fee_rate.maker_fee_rate,
        exit_mode=exit_mode,
        runner_admission_reasons=runner_admission_reasons,
        liquidation_safety_reason=liquidation_safety_reason,
        stop_to_liquidation_r=stop_to_liquidation_r,
        live_mainnet_order_routing_allowed=False,
    )
