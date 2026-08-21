from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_excursion_store import BybitDemoExcursionCheckpoint
from app.execution.bybit_entry_recovery import BybitEntryRecoveryRecord
from app.execution.bybit_entry_restart_recovery import (
    BybitExecutedEntryRecoveryResult,
    BybitExecutedEntryRecoveryStatus,
    build_recovered_entry_excursion_state,
    execute_bybit_executed_entry_recovery,
    plan_bybit_executed_entry_recovery,
)
from app.execution.bybit_order_lookup import BybitOrderTruth
from app.execution.bybit_startup_reconciliation import (
    BybitStartupReconciliationResult,
    BybitStartupReconciliationStatus,
)
from app.oms.store import OrderRecord, OrderState


class BybitEntryRecoveryConvergenceStatus(StrEnum):
    ACTIVE_MANAGEMENT_READY = "ACTIVE_MANAGEMENT_READY"
    TERMINAL_HANDOFF_REQUIRED = "TERMINAL_HANDOFF_REQUIRED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class BybitEntryRecoveryConvergenceResult:
    status: BybitEntryRecoveryConvergenceStatus
    reasons: tuple[str, ...]
    checkpoint: BybitDemoExcursionCheckpoint | None
    safety_result: BybitExecutedEntryRecoveryResult | None
    oms_record: OrderRecord | None
    stale_lease_recovered: bool
    runtime_lease_acquired: bool
    runtime_lease_released: bool
    next_entry_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


class _RecoveryRecordStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool
    immutable_records: bool

    def load(self, *, entry_order_link_id: str) -> BybitEntryRecoveryRecord: ...


class _RuntimeLease(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool
    automatic_stale_takeover_allowed: bool

    def acquire(self): ...

    def inspect(self): ...

    def recover_expired(
        self,
        *,
        expected_fencing_token: int,
        broker_reconciliation: BybitStartupReconciliationResult,
        operator_reason: str,
    ) -> None: ...

    def release(self, *, owner_token: str) -> None: ...


class _ExcursionStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool

    def load(self) -> BybitDemoExcursionCheckpoint: ...

    def initialize(self, *, entry_order_link_id: str, state): ...


class _EntryOms(Protocol):
    live_mainnet_order_routing_allowed: bool

    def get(self, intent_id: str) -> OrderRecord | None: ...

    def mark_lifecycle_reconciliation_required(
        self,
        intent_id: str,
        *,
        broker_order_id: str,
        broker_status: str,
        cumulative_executed_quantity,
        occurred_at: datetime,
    ) -> OrderRecord: ...

    def transition(
        self,
        intent_id: str,
        target: OrderState,
        *,
        event_id: str,
        occurred_at: datetime,
        broker_order_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> OrderRecord: ...

    def apply_cumulative_fill(
        self,
        intent_id: str,
        *,
        event_id: str,
        cumulative_filled,
        occurred_at: datetime,
        broker_order_id: str | None = None,
    ) -> OrderRecord: ...


def converge_bybit_executed_entry_recovery(
    record: OrderRecord,
    *,
    order_truth: BybitOrderTruth,
    positions: tuple[BybitDemoPosition, ...],
    recovery_store: _RecoveryRecordStore,
    runtime_lease: _RuntimeLease,
    excursion_store: _ExcursionStore,
    entry_oms: _EntryOms,
    client: Any,
    occurred_at: datetime | None = None,
) -> BybitEntryRecoveryConvergenceResult:
    """Converge crash-after-ENTRY into normal checkpoint + OMS state without another ENTRY POST.

    Recovery is allowed to retire an *expired* stale lease only after immutable envelope, broker
    order and broker position truth agree. The generic runtime still advertises automatic stale
    takeover as forbidden; this is a narrowly scoped safety-recovery path tied to one deterministic
    executed ENTRY. An unexpired owner is never preempted.
    """

    moment = datetime.now(UTC) if occurred_at is None else occurred_at
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("entry recovery convergence timestamp must be timezone-aware")
    moment = moment.astimezone(UTC)
    _validate_dependencies(
        recovery_store=recovery_store,
        runtime_lease=runtime_lease,
        excursion_store=excursion_store,
        entry_oms=entry_oms,
        client=client,
    )
    if record.client_order_id != order_truth.order_link_id:
        return _blocked("RECOVERY_OMS_ORDER_LINK_ID_MISMATCH")

    try:
        existing_checkpoint = excursion_store.load()
    except FileNotFoundError:
        existing_checkpoint = None
    except Exception as exc:  # noqa: BLE001 - unreadable durable state cannot be overwritten.
        return _blocked(f"RECOVERY_CHECKPOINT_READ_FAILED:{type(exc).__name__}")
    if (
        existing_checkpoint is not None
        and existing_checkpoint.entry_order_link_id != record.client_order_id
    ):
        return _blocked("RECOVERY_FOREIGN_ACTIVE_CHECKPOINT_PRESENT")

    try:
        recovery = recovery_store.load(entry_order_link_id=record.client_order_id)
    except Exception as exc:  # noqa: BLE001 - frozen inputs are mandatory.
        return _blocked(f"RECOVERY_ENVELOPE_LOAD_FAILED:{type(exc).__name__}")

    active_positions = tuple(position for position in positions if position.size > 0)
    if existing_checkpoint is not None and not active_positions:
        lease = _acquire_recovery_lease(
            runtime_lease,
            proof=_lease_recovery_proof(
                checkpoint=existing_checkpoint,
                positions=(),
                terminal=True,
            ),
        )
        if isinstance(lease, BybitEntryRecoveryConvergenceResult):
            return lease
        lease_record, stale_recovered = lease
        try:
            oms = _converge_entry_oms(
                entry_oms,
                record=record,
                truth=order_truth,
                occurred_at=moment,
            )
        except Exception as exc:  # noqa: BLE001 - checkpoint keeps re-entry blocked.
            return _release_after_block(
                runtime_lease,
                lease_record,
                stale_recovered=stale_recovered,
                reason=f"RECOVERY_OMS_CONVERGENCE_FAILED:{type(exc).__name__}",
                checkpoint=existing_checkpoint,
            )
        return _release_success(
            runtime_lease,
            lease_record,
            status=BybitEntryRecoveryConvergenceStatus.TERMINAL_HANDOFF_REQUIRED,
            checkpoint=existing_checkpoint,
            safety_result=None,
            oms_record=oms,
            stale_recovered=stale_recovered,
        )

    try:
        plan = plan_bybit_executed_entry_recovery(
            recovery,
            order_truth=order_truth,
            positions=positions,
        )
    except Exception as exc:  # noqa: BLE001 - broker/envelope drift is fail-closed.
        return _blocked(f"RECOVERY_PLAN_REJECTED:{type(exc).__name__}")

    proof_checkpoint = existing_checkpoint
    if proof_checkpoint is None:
        proof = _lease_recovery_proof(checkpoint=None, positions=(plan.position,), terminal=False)
    else:
        proof = _lease_recovery_proof(
            checkpoint=proof_checkpoint,
            positions=(plan.position,),
            terminal=False,
        )
    lease = _acquire_recovery_lease(runtime_lease, proof=proof)
    if isinstance(lease, BybitEntryRecoveryConvergenceResult):
        return lease
    lease_record, stale_recovered = lease

    try:
        safety = execute_bybit_executed_entry_recovery(plan, client=client)
    except Exception as exc:  # noqa: BLE001 - unexpected safety failure blocks convergence.
        return _release_after_block(
            runtime_lease,
            lease_record,
            stale_recovered=stale_recovered,
            reason=f"RECOVERY_SAFETY_EXECUTION_FAILED:{type(exc).__name__}",
            checkpoint=existing_checkpoint,
        )
    if safety.status is BybitExecutedEntryRecoveryStatus.UNRESOLVED:
        return _release_after_block(
            runtime_lease,
            lease_record,
            stale_recovered=stale_recovered,
            reason="RECOVERY_SAFETY_STATE_UNRESOLVED",
            checkpoint=existing_checkpoint,
            safety_result=safety,
        )

    checkpoint = existing_checkpoint
    if checkpoint is None:
        try:
            state = build_recovered_entry_excursion_state(safety)
            checkpoint = excursion_store.initialize(
                entry_order_link_id=record.client_order_id,
                state=state,
            )
        except Exception as exc:  # noqa: BLE001 - protected exposure without checkpoint blocks.
            return _release_after_block(
                runtime_lease,
                lease_record,
                stale_recovered=stale_recovered,
                reason=f"RECOVERY_CHECKPOINT_INITIALIZE_FAILED:{type(exc).__name__}",
                checkpoint=None,
                safety_result=safety,
            )

    try:
        oms = _converge_entry_oms(
            entry_oms,
            record=record,
            truth=order_truth,
            occurred_at=moment,
        )
    except Exception as exc:  # noqa: BLE001 - checkpoint prevents replacement entry.
        return _release_after_block(
            runtime_lease,
            lease_record,
            stale_recovered=stale_recovered,
            reason=f"RECOVERY_OMS_CONVERGENCE_FAILED:{type(exc).__name__}",
            checkpoint=checkpoint,
            safety_result=safety,
        )

    status = (
        BybitEntryRecoveryConvergenceStatus.ACTIVE_MANAGEMENT_READY
        if safety.status is BybitExecutedEntryRecoveryStatus.PROTECTED
        else BybitEntryRecoveryConvergenceStatus.TERMINAL_HANDOFF_REQUIRED
    )
    return _release_success(
        runtime_lease,
        lease_record,
        status=status,
        checkpoint=checkpoint,
        safety_result=safety,
        oms_record=oms,
        stale_recovered=stale_recovered,
    )


def _converge_entry_oms(
    entry_oms: _EntryOms,
    *,
    record: OrderRecord,
    truth: BybitOrderTruth,
    occurred_at: datetime,
) -> OrderRecord:
    current = entry_oms.get(record.intent_id)
    if current is None:
        raise KeyError(record.intent_id)
    if current.client_order_id != truth.order_link_id:
        raise ValueError("recovery OMS orderLinkId changed")
    if current.broker_order_id not in {"", truth.order_id}:
        raise ValueError("recovery OMS broker order id changed")

    if current.state in {OrderState.SUBMIT_STARTED, OrderState.UNCERTAIN, OrderState.RECONCILING}:
        current = entry_oms.mark_lifecycle_reconciliation_required(
            current.intent_id,
            broker_order_id=truth.order_id,
            broker_status=truth.status,
            cumulative_executed_quantity=truth.cumulative_executed_quantity,
            occurred_at=occurred_at,
        )
    if current.state is OrderState.RECONCILING:
        current = entry_oms.transition(
            current.intent_id,
            OrderState.RECONCILED,
            event_id=f"bybit-recovery-reconciled:{current.intent_id}:{truth.order_id}",
            occurred_at=occurred_at,
            broker_order_id=truth.order_id,
            payload={
                "broker_status": truth.status,
                "cumulative_executed_quantity": str(truth.cumulative_executed_quantity),
                "executed_entry_recovery": True,
            },
        )

    if current.state in {
        OrderState.RECONCILED,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
    }:
        current = entry_oms.apply_cumulative_fill(
            current.intent_id,
            event_id=(
                f"bybit-recovery-fill:{current.intent_id}:{truth.order_id}:"
                f"{truth.cumulative_executed_quantity}"
            ),
            cumulative_filled=truth.cumulative_executed_quantity,
            occurred_at=occurred_at,
            broker_order_id=truth.order_id,
        )

    if truth.status == "Cancelled":
        if current.state is OrderState.PARTIALLY_FILLED:
            current = entry_oms.transition(
                current.intent_id,
                OrderState.CANCELLED,
                event_id=f"bybit-recovery-cancelled:{current.intent_id}:{truth.order_id}",
                occurred_at=occurred_at,
                broker_order_id=truth.order_id,
                payload={
                    "broker_status": truth.status,
                    "executed_entry_recovery": True,
                },
            )
    elif truth.status == "Filled":
        if current.state is not OrderState.FILLED:
            raise ValueError("filled broker ENTRY did not converge OMS to FILLED")
    else:
        raise ValueError("executed ENTRY broker status is not terminal for recovery")

    if current.filled_quantity != truth.cumulative_executed_quantity:
        raise ValueError("recovery OMS filled quantity does not match broker truth")
    return current


def _acquire_recovery_lease(
    runtime_lease: _RuntimeLease,
    *,
    proof: BybitStartupReconciliationResult,
):
    try:
        return runtime_lease.acquire(), False
    except FileExistsError:
        try:
            stale = runtime_lease.inspect()
            runtime_lease.recover_expired(
                expected_fencing_token=stale.fencing_token,
                broker_reconciliation=proof,
                operator_reason=(
                    "deterministic executed-entry crash recovery after immutable broker proof"
                ),
            )
            return runtime_lease.acquire(), True
        except Exception as exc:  # noqa: BLE001 - active/unverifiable owner cannot be preempted.
            return _blocked(f"RECOVERY_RUNTIME_LEASE_UNAVAILABLE:{type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - malformed lease authority blocks recovery.
        return _blocked(f"RECOVERY_RUNTIME_LEASE_ACQUIRE_FAILED:{type(exc).__name__}")


def _lease_recovery_proof(
    *,
    checkpoint: BybitDemoExcursionCheckpoint | None,
    positions: tuple[BybitDemoPosition, ...],
    terminal: bool,
) -> BybitStartupReconciliationResult:
    status = (
        BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED
        if terminal
        else BybitStartupReconciliationStatus.RESUME_MANAGEMENT
    )
    return BybitStartupReconciliationResult(
        status=status,
        reasons=("EXECUTED_ENTRY_IMMUTABLE_RECOVERY_PROOF",),
        checkpoint=checkpoint,
        active_positions=positions,
        open_orders=(),
        next_entry_allowed=False,
        management_allowed=not terminal,
        terminal_recovery_required=terminal,
        broker_truth_complete=True,
    )


def _release_success(
    runtime_lease: _RuntimeLease,
    lease_record,
    *,
    status: BybitEntryRecoveryConvergenceStatus,
    checkpoint: BybitDemoExcursionCheckpoint,
    safety_result: BybitExecutedEntryRecoveryResult | None,
    oms_record: OrderRecord,
    stale_recovered: bool,
) -> BybitEntryRecoveryConvergenceResult:
    try:
        runtime_lease.release(owner_token=lease_record.owner_token)
    except Exception as exc:  # noqa: BLE001 - uncertain fence blocks product resumption.
        return BybitEntryRecoveryConvergenceResult(
            status=BybitEntryRecoveryConvergenceStatus.BLOCKED,
            reasons=(f"RECOVERY_RUNTIME_LEASE_RELEASE_FAILED:{type(exc).__name__}",),
            checkpoint=checkpoint,
            safety_result=safety_result,
            oms_record=oms_record,
            stale_lease_recovered=stale_recovered,
            runtime_lease_acquired=True,
            runtime_lease_released=False,
        )
    return BybitEntryRecoveryConvergenceResult(
        status=status,
        reasons=(),
        checkpoint=checkpoint,
        safety_result=safety_result,
        oms_record=oms_record,
        stale_lease_recovered=stale_recovered,
        runtime_lease_acquired=True,
        runtime_lease_released=True,
    )


def _release_after_block(
    runtime_lease: _RuntimeLease,
    lease_record,
    *,
    stale_recovered: bool,
    reason: str,
    checkpoint: BybitDemoExcursionCheckpoint | None,
    safety_result: BybitExecutedEntryRecoveryResult | None = None,
) -> BybitEntryRecoveryConvergenceResult:
    released = False
    reasons = [reason]
    try:
        runtime_lease.release(owner_token=lease_record.owner_token)
        released = True
    except Exception as exc:  # noqa: BLE001 - retain both primary and lease uncertainty.
        reasons.append(f"RECOVERY_RUNTIME_LEASE_RELEASE_FAILED:{type(exc).__name__}")
    return BybitEntryRecoveryConvergenceResult(
        status=BybitEntryRecoveryConvergenceStatus.BLOCKED,
        reasons=tuple(reasons),
        checkpoint=checkpoint,
        safety_result=safety_result,
        oms_record=None,
        stale_lease_recovered=stale_recovered,
        runtime_lease_acquired=True,
        runtime_lease_released=released,
    )


def _blocked(reason: str) -> BybitEntryRecoveryConvergenceResult:
    return BybitEntryRecoveryConvergenceResult(
        status=BybitEntryRecoveryConvergenceStatus.BLOCKED,
        reasons=(reason,),
        checkpoint=None,
        safety_result=None,
        oms_record=None,
        stale_lease_recovered=False,
        runtime_lease_acquired=False,
        runtime_lease_released=False,
    )


def _validate_dependencies(
    *,
    recovery_store: _RecoveryRecordStore,
    runtime_lease: _RuntimeLease,
    excursion_store: _ExcursionStore,
    entry_oms: _EntryOms,
    client: Any,
) -> None:
    for name, value in (
        ("recovery store", recovery_store),
        ("runtime lease", runtime_lease),
        ("excursion store", excursion_store),
        ("entry OMS", entry_oms),
        ("recovery client", client),
    ):
        if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
            raise ValueError(f"entry recovery convergence rejected mainnet-capable {name}")
    if recovery_store.order_writes_supported:
        raise ValueError("entry recovery record store must not expose broker order writes")
    if not recovery_store.immutable_records:
        raise ValueError("entry recovery record store must be immutable")
    if runtime_lease.order_writes_supported:
        raise ValueError("entry recovery runtime lease must not expose broker order writes")
    if runtime_lease.automatic_stale_takeover_allowed:
        raise ValueError(
            "entry recovery requires generic automatic stale takeover to stay disabled"
        )
    if excursion_store.order_writes_supported:
        raise ValueError("entry recovery excursion store must be diagnostics-only")
