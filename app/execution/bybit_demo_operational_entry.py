from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_approved_runtime import (
    BybitDemoOperatorApprovedTradingRuntimeResult,
    run_operator_approved_bybit_demo_trading_runtime,
)
from app.execution.bybit_demo_connected_preflight import BybitDemoConnectedPreflightResult
from app.execution.bybit_demo_control_plane import (
    BybitDemoControlDecision,
    BybitDemoControlMode,
)
from app.execution.bybit_demo_fixed_egress import require_fixed_egress_ready_for_arm
from app.execution.bybit_demo_operator_approval import BybitDemoOperatorApproval
from app.execution.bybit_demo_trading_runtime import BybitDemoTradingRuntimeStatus
from app.marketdata.bybit_v5 import BybitKlineBar


class BybitDemoOperationalProtectionStatus(StrEnum):
    CANONICAL_RUNTIME_RECONCILED = "CANONICAL_RUNTIME_RECONCILED"
    NO_ENTRY_AUTHORIZATION = "NO_ENTRY_AUTHORIZATION"
    NO_EXECUTION_CONFIRMED = "NO_EXECUTION_CONFIRMED"
    RECOVERED_PROTECTED = "RECOVERED_PROTECTED"
    RECOVERED_FLATTENED = "RECOVERED_FLATTENED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class BybitDemoOperationalProtectionReconciliation:
    status: BybitDemoOperationalProtectionStatus
    completed: bool
    entry_execution_confirmed: bool | None
    safety_mutation_performed: bool
    second_entry_submit_performed: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        if self.second_entry_submit_performed:
            raise ValueError("operational reconciliation cannot submit a second entry")
        if self.live_mainnet_order_routing_allowed:
            raise ValueError("operational reconciliation cannot route mainnet orders")
        if self.completed and self.status is BybitDemoOperationalProtectionStatus.UNRESOLVED:
            raise ValueError("unresolved operational reconciliation cannot be complete")
        if not self.completed and self.status is not BybitDemoOperationalProtectionStatus.UNRESOLVED:
            raise ValueError("incomplete operational reconciliation must remain unresolved")


class BybitDemoOperationalEntryStatus(StrEnum):
    ENTRY_CYCLE_COMPLETE = "ENTRY_CYCLE_COMPLETE"
    ENTRY_BLOCKED = "ENTRY_BLOCKED"


@dataclass(frozen=True)
class BybitDemoOperationalEntryEvidence:
    status: BybitDemoOperationalEntryStatus
    observed_at: datetime
    approval_id: str
    source_snapshot_id: str
    source_evidence_rank: int
    symbol: str
    side: str
    entry_order_link_id: str
    pinned_control_event_id: str
    pinned_control_armed_until: datetime
    runtime_status: str | None
    runtime_error_type: str | None
    authorization_persisted: bool
    authorization_record_sha256: str | None
    entry_provenance_persisted: bool
    entry_provenance_record_sha256: str | None
    protection_reconciliation_status: BybitDemoOperationalProtectionStatus
    protection_reconciliation_completed: bool
    same_invocation_additional_entry_allowed: bool
    fixed_egress_verified: bool = True
    protected_dispatch_required: bool = True
    automatic_arm_allowed: bool = False
    ranked_fallback_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_OPERATIONAL_ENTRY_EVIDENCE_V1",
            "status": self.status.value,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "approval_id": self.approval_id,
            "source_snapshot_id": self.source_snapshot_id,
            "source_evidence_rank": self.source_evidence_rank,
            "symbol": self.symbol,
            "side": self.side,
            "entry_order_link_id": self.entry_order_link_id,
            "pinned_control_event_id": self.pinned_control_event_id,
            "pinned_control_armed_until": self.pinned_control_armed_until.astimezone(UTC).isoformat(),
            "runtime_status": self.runtime_status,
            "runtime_error_type": self.runtime_error_type,
            "authorization_persisted": self.authorization_persisted,
            "authorization_record_sha256": self.authorization_record_sha256,
            "entry_provenance_persisted": self.entry_provenance_persisted,
            "entry_provenance_record_sha256": self.entry_provenance_record_sha256,
            "protection_reconciliation_status": self.protection_reconciliation_status.value,
            "protection_reconciliation_completed": self.protection_reconciliation_completed,
            "same_invocation_additional_entry_allowed": self.same_invocation_additional_entry_allowed,
            "fixed_egress_verified": self.fixed_egress_verified,
            "protected_dispatch_required": self.protected_dispatch_required,
            "automatic_arm_allowed": self.automatic_arm_allowed,
            "ranked_fallback_allowed": self.ranked_fallback_allowed,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


PostAttemptReconciler = Callable[
    [BybitDemoOperatorApproval, BybitDemoOperatorApprovedTradingRuntimeResult | None],
    BybitDemoOperationalProtectionReconciliation,
]
RuntimeRunner = Callable[..., BybitDemoOperatorApprovedTradingRuntimeResult]


class PinnedBybitDemoControlPlane:
    """Read-only view that requires every v121 read to resolve to one exact ARM event."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    immutable_records = True
    fixed_egress_required = True

    def __init__(
        self,
        control_plane: Any,
        decision: BybitDemoControlDecision,
    ) -> None:
        _validate_control_plane(control_plane)
        _validate_armed_decision(decision, now=None)
        if decision.latest_event_id is None or decision.armed_until is None:
            raise ValueError("pinned Bybit Demo ARM identity is incomplete")
        self._control_plane = control_plane
        self._event_id = decision.latest_event_id
        self._event_kind = decision.latest_event_kind
        self._armed_until = decision.armed_until.astimezone(UTC)

    @property
    def pinned_event_id(self) -> str:
        return self._event_id

    @property
    def pinned_armed_until(self) -> datetime:
        return self._armed_until

    def read_decision(self, *, now: datetime) -> BybitDemoControlDecision:
        moment = _utc(now, "pinned control read time")
        decision = self._control_plane.read_decision(now=moment)
        _validate_armed_decision(decision, now=moment)
        if decision.latest_event_id != self._event_id:
            raise RuntimeError("BYBIT_DEMO_PINNED_ARM_EVENT_CHANGED")
        if decision.latest_event_kind != self._event_kind:
            raise RuntimeError("BYBIT_DEMO_PINNED_ARM_EVENT_KIND_CHANGED")
        if decision.armed_until is None or decision.armed_until.astimezone(UTC) != self._armed_until:
            raise RuntimeError("BYBIT_DEMO_PINNED_ARM_EXPIRY_CHANGED")
        return decision


def run_protected_bybit_demo_operational_entry(
    approval: BybitDemoOperatorApproval,
    latest_review_row: Mapping[str, Any],
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    fixed_egress_preflight: BybitDemoConnectedPreflightResult,
    new_entry_control_plane: Any,
    post_attempt_reconciler: PostAttemptReconciler,
    now: datetime,
    control_now_provider: Callable[[], datetime] | None = None,
    runtime_runner: RuntimeRunner = run_operator_approved_bybit_demo_trading_runtime,
    **runtime_kwargs: Any,
) -> BybitDemoOperationalEntryEvidence:
    """Delegate one protected Demo entry invocation to the already-qualified approved runtime.

    This layer never arms v121, never selects a replacement opportunity and never owns order
    construction. It proves fixed egress, pins one already-existing ARM event, rejects a previously
    burned deterministic entry identity, delegates the canonical runtime exactly once and then
    requires a post-attempt protection reconciliation result before emitting allowlisted evidence.
    """

    moment = _utc(now, "operational entry time")
    require_fixed_egress_ready_for_arm(fixed_egress_preflight)
    approval.validate(now=moment)
    _validate_runtime_dependencies(approval, runtime_kwargs)
    _reject_existing_lineage(approval, runtime_kwargs)

    _validate_control_plane(new_entry_control_plane)
    initial_decision = new_entry_control_plane.read_decision(now=moment)
    _validate_armed_decision(initial_decision, now=moment)
    pinned_control = PinnedBybitDemoControlPlane(new_entry_control_plane, initial_decision)

    active_now_provider = (
        (lambda: datetime.now(UTC))
        if control_now_provider is None
        else control_now_provider
    )
    runtime_result: BybitDemoOperatorApprovedTradingRuntimeResult | None = None
    runtime_error_type: str | None = None
    try:
        runtime_result = runtime_runner(
            approval,
            latest_review_row,
            bars_by_symbol,
            now=moment,
            new_entry_control_plane=pinned_control,
            control_now_provider=active_now_provider,
            **runtime_kwargs,
        )
        _validate_runtime_result(approval, runtime_result)
    except Exception as exc:  # noqa: BLE001 - evidence exposes only the exception class.
        runtime_error_type = type(exc).__name__

    reconciliation: BybitDemoOperationalProtectionReconciliation
    try:
        reconciliation = post_attempt_reconciler(approval, runtime_result)
        reconciliation.validate()
    except Exception:  # noqa: BLE001 - safety uncertainty must remain fail-closed and sanitized.
        reconciliation = BybitDemoOperationalProtectionReconciliation(
            status=BybitDemoOperationalProtectionStatus.UNRESOLVED,
            completed=False,
            entry_execution_confirmed=None,
            safety_mutation_performed=False,
        )

    return _build_evidence(
        approval=approval,
        pinned_control=pinned_control,
        runtime_result=runtime_result,
        runtime_error_type=runtime_error_type,
        reconciliation=reconciliation,
        observed_at=_utc(active_now_provider(), "operational evidence time"),
    )


def _validate_runtime_dependencies(
    approval: BybitDemoOperatorApproval,
    runtime_kwargs: Mapping[str, Any],
) -> None:
    client = runtime_kwargs.get("client")
    if getattr(client, "environment", None) != "BYBIT_DEMO":
        raise ValueError("operational entry requires BYBIT_DEMO order client")
    if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational entry rejected mainnet-capable order client")
    if getattr(client, "entry_recovery_required", False) is not True:
        raise ValueError("operational entry requires durable at-most-once entry recovery client")
    entry_oms = getattr(client, "entry_oms", None)
    if entry_oms is None:
        raise ValueError("operational entry requires canonical entry OMS")
    if getattr(entry_oms, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational entry rejected mainnet-capable entry OMS")
    if getattr(entry_oms, "automatic_resubmit_after_submit_started_allowed", True) is not False:
        raise ValueError("operational entry forbids automatic entry resubmit")

    authorization_store = runtime_kwargs.get("approval_authorization_store")
    if authorization_store is None or not callable(getattr(authorization_store, "load", None)):
        raise ValueError("operational entry requires durable authorization store")
    if getattr(authorization_store, "immutable_records", False) is not True:
        raise ValueError("operational entry authorization store must be immutable")
    if getattr(authorization_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational entry rejected mainnet-capable authorization store")

    provenance_store = runtime_kwargs.get("entry_provenance_store")
    if provenance_store is None or not callable(getattr(provenance_store, "load", None)):
        raise ValueError("operational entry requires durable entry provenance store")
    if getattr(provenance_store, "immutable_records", False) is not True:
        raise ValueError("operational entry provenance store must be immutable")
    if getattr(provenance_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational entry rejected mainnet-capable provenance store")

    if approval.environment != "BYBIT_DEMO" or approval.live_mainnet_order_routing_allowed:
        raise ValueError("operational entry rejected unsafe approval environment")


def _reject_existing_lineage(
    approval: BybitDemoOperatorApproval,
    runtime_kwargs: Mapping[str, Any],
) -> None:
    entry_id = approval.expected_entry_order_link_id
    for role, store in (
        ("AUTHORIZATION", runtime_kwargs["approval_authorization_store"]),
        ("PROVENANCE", runtime_kwargs["entry_provenance_store"]),
    ):
        try:
            record = store.load(entry_order_link_id=entry_id)
        except FileNotFoundError:
            continue
        if getattr(record, "live_mainnet_order_routing_allowed", False) is True:
            raise ValueError(f"BYBIT_DEMO_EXISTING_{role}_REJECTED_MAINNET_CAPABILITY")
        raise RuntimeError(f"BYBIT_DEMO_ENTRY_{role}_ALREADY_EXISTS")


def _validate_control_plane(control_plane: Any) -> None:
    if getattr(control_plane, "fixed_egress_required", False) is not True:
        raise ValueError("operational entry requires fixed-egress v121 control plane")
    if getattr(control_plane, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational entry rejected mainnet-capable control plane")
    if getattr(control_plane, "order_writes_supported", True) is not False:
        raise ValueError("operational entry control plane cannot write orders")
    if getattr(control_plane, "order_submission_supported", True) is not False:
        raise ValueError("operational entry control plane cannot submit orders")
    if getattr(control_plane, "immutable_records", False) is not True:
        raise ValueError("operational entry requires immutable v121 control records")
    if not callable(getattr(control_plane, "read_decision", None)):
        raise ValueError("operational entry control plane requires read_decision")


def _validate_armed_decision(
    decision: BybitDemoControlDecision,
    *,
    now: datetime | None,
) -> None:
    if decision.live_mainnet_order_routing_allowed or decision.order_writes_supported:
        raise ValueError("operational entry rejected unsafe v121 control decision")
    if decision.mode is not BybitDemoControlMode.ARMED_NEW_ENTRIES:
        raise RuntimeError("BYBIT_DEMO_EXISTING_V121_ARM_REQUIRED")
    if decision.new_entry_allowed is not True or decision.reasons:
        raise RuntimeError("BYBIT_DEMO_EXISTING_V121_ARM_NOT_ENTRY_READY")
    if decision.latest_event_kind != "ARM_NEW_ENTRIES":
        raise RuntimeError("BYBIT_DEMO_EXISTING_V121_ARM_EVENT_KIND_INVALID")
    if decision.latest_event_id is None or len(decision.latest_event_id) != 64:
        raise RuntimeError("BYBIT_DEMO_EXISTING_V121_ARM_EVENT_ID_INVALID")
    if any(character not in "0123456789abcdef" for character in decision.latest_event_id):
        raise RuntimeError("BYBIT_DEMO_EXISTING_V121_ARM_EVENT_ID_INVALID")
    if decision.armed_until is None:
        raise RuntimeError("BYBIT_DEMO_EXISTING_V121_ARM_EXPIRY_MISSING")
    armed_until = _utc(decision.armed_until, "v121 ARM expiry")
    if now is not None and _utc(now, "v121 ARM observation time") >= armed_until:
        raise RuntimeError("BYBIT_DEMO_EXISTING_V121_ARM_EXPIRED")


def _validate_runtime_result(
    approval: BybitDemoOperatorApproval,
    result: BybitDemoOperatorApprovedTradingRuntimeResult,
) -> None:
    if result.live_mainnet_order_routing_allowed or not result.demo_only:
        raise ValueError("operational entry rejected unsafe approved runtime result")
    authorization = result.authorization
    receipt = result.authorization_receipt
    if authorization is not None:
        if authorization.approval_id != approval.approval_id:
            raise ValueError("operational entry authorization approval id mismatch")
        if authorization.expected_entry_order_link_id != approval.expected_entry_order_link_id:
            raise ValueError("operational entry authorization orderLinkId mismatch")
    if result.authorization_persisted != (receipt is not None):
        raise ValueError("operational entry authorization persistence evidence mismatch")
    if receipt is not None:
        if receipt.approval_id != approval.approval_id:
            raise ValueError("operational entry authorization receipt approval id mismatch")
        if receipt.entry_order_link_id != approval.expected_entry_order_link_id:
            raise ValueError("operational entry authorization receipt orderLinkId mismatch")

    runtime = result.runtime_result
    if runtime.live_mainnet_order_routing_allowed or not runtime.demo_only:
        raise ValueError("operational entry rejected unsafe canonical runtime result")
    if runtime.same_invocation_additional_entry_allowed:
        raise ValueError("operational entry runtime allowed an additional same-invocation entry")
    provenance = runtime.entry_provenance
    provenance_receipt = runtime.entry_provenance_receipt
    if runtime.entry_provenance_persisted != (provenance_receipt is not None):
        raise ValueError("operational entry provenance persistence evidence mismatch")
    if provenance is not None:
        if provenance.entry_order_link_id != approval.expected_entry_order_link_id:
            raise ValueError("operational entry provenance orderLinkId mismatch")
    if provenance_receipt is not None:
        if provenance_receipt.entry_order_link_id != approval.expected_entry_order_link_id:
            raise ValueError("operational entry provenance receipt orderLinkId mismatch")


def _build_evidence(
    *,
    approval: BybitDemoOperatorApproval,
    pinned_control: PinnedBybitDemoControlPlane,
    runtime_result: BybitDemoOperatorApprovedTradingRuntimeResult | None,
    runtime_error_type: str | None,
    reconciliation: BybitDemoOperationalProtectionReconciliation,
    observed_at: datetime,
) -> BybitDemoOperationalEntryEvidence:
    runtime_status = None
    authorization_persisted = False
    authorization_sha = None
    provenance_persisted = False
    provenance_sha = None
    additional_entry_allowed = False
    if runtime_result is not None:
        runtime = runtime_result.runtime_result
        runtime_status = runtime.status.value
        authorization_persisted = runtime_result.authorization_persisted
        if runtime_result.authorization_receipt is not None:
            authorization_sha = runtime_result.authorization_receipt.record_sha256
        provenance_persisted = runtime.entry_provenance_persisted
        if runtime.entry_provenance_receipt is not None:
            provenance_sha = runtime.entry_provenance_receipt.record_sha256
        additional_entry_allowed = runtime.same_invocation_additional_entry_allowed

    complete = (
        runtime_error_type is None
        and runtime_status == BybitDemoTradingRuntimeStatus.ENTRY_CYCLE_EXECUTED.value
        and reconciliation.completed
        and reconciliation.status
        is BybitDemoOperationalProtectionStatus.CANONICAL_RUNTIME_RECONCILED
        and authorization_persisted
        and provenance_persisted
        and not additional_entry_allowed
    )
    return BybitDemoOperationalEntryEvidence(
        status=(
            BybitDemoOperationalEntryStatus.ENTRY_CYCLE_COMPLETE
            if complete
            else BybitDemoOperationalEntryStatus.ENTRY_BLOCKED
        ),
        observed_at=observed_at,
        approval_id=approval.approval_id,
        source_snapshot_id=approval.source_snapshot_id,
        source_evidence_rank=approval.source_evidence_rank,
        symbol=approval.symbol,
        side=approval.side,
        entry_order_link_id=approval.expected_entry_order_link_id,
        pinned_control_event_id=pinned_control.pinned_event_id,
        pinned_control_armed_until=pinned_control.pinned_armed_until,
        runtime_status=runtime_status,
        runtime_error_type=runtime_error_type,
        authorization_persisted=authorization_persisted,
        authorization_record_sha256=authorization_sha,
        entry_provenance_persisted=provenance_persisted,
        entry_provenance_record_sha256=provenance_sha,
        protection_reconciliation_status=reconciliation.status,
        protection_reconciliation_completed=reconciliation.completed,
        same_invocation_additional_entry_allowed=additional_entry_allowed,
    )


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "BybitDemoOperationalEntryEvidence",
    "BybitDemoOperationalEntryStatus",
    "BybitDemoOperationalProtectionReconciliation",
    "BybitDemoOperationalProtectionStatus",
    "PinnedBybitDemoControlPlane",
    "run_protected_bybit_demo_operational_entry",
]
