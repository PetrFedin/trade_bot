from __future__ import annotations

from typing import Any

from app.execution.bybit_demo_operational_entry import (
    BybitDemoOperationalProtectionReconciliation,
    BybitDemoOperationalProtectionStatus,
)
from app.execution.bybit_demo_operator_approval import BybitDemoOperatorApproval
from app.execution.bybit_entry_restart_recovery import (
    BybitExecutedEntryRecoveryStatus,
    execute_bybit_executed_entry_recovery,
    plan_bybit_executed_entry_recovery,
)
from app.execution.bybit_order_lookup import lookup_bybit_order_by_link_id
from app.oms.bybit_entry import bybit_entry_intent_id
from app.oms.store import OrderState

_LOCAL_ZERO_FILL_STATES = frozenset(
    {
        OrderState.CREATED,
        OrderState.RISK_APPROVED,
        OrderState.OUTBOXED,
        OrderState.REJECTED,
    }
)


def reconcile_protected_bybit_demo_entry_attempt(
    approval: BybitDemoOperatorApproval,
    runtime_result: Any | None,
    *,
    authorization_store: Any,
    entry_oms: Any,
    recovery_store: Any,
    broker_client: Any,
) -> BybitDemoOperationalProtectionReconciliation:
    """Resolve one authorized Demo ENTRY attempt without ever submitting another ENTRY.

    A canonical successful runtime has already reconciled exchange-native protection before it can
    persist entry provenance, so that path is acknowledged without issuing a second protection
    mutation. If the runtime failed after authorization was burned, this function reconstructs
    broker truth by deterministic orderLinkId. Positive execution can only flow through the existing
    frozen-envelope safety recovery, which may protect the position or perform a deterministic
    reduce-only flatten. No strategy selection, ARM mutation or risk-adding order is available here.
    """

    approval.validate()
    _validate_dependencies(
        authorization_store=authorization_store,
        entry_oms=entry_oms,
        recovery_store=recovery_store,
        broker_client=broker_client,
    )

    canonical_authorization_sha = _canonical_runtime_authorization_sha(
        approval,
        runtime_result,
    )
    if _canonical_runtime_proved_protected_entry(approval, runtime_result):
        return _result(
            BybitDemoOperationalProtectionStatus.CANONICAL_RUNTIME_RECONCILED,
            completed=True,
            entry_execution_confirmed=True,
            safety_mutation_performed=False,
            authorization_persisted=True,
            authorization_record_sha256=canonical_authorization_sha,
        )

    entry_order_link_id = approval.expected_entry_order_link_id
    try:
        authorization_record = authorization_store.load(
            entry_order_link_id=entry_order_link_id
        )
    except FileNotFoundError:
        return _result(
            BybitDemoOperationalProtectionStatus.NO_ENTRY_AUTHORIZATION,
            completed=True,
            entry_execution_confirmed=False,
            safety_mutation_performed=False,
        )
    _validate_authorization_record(approval, authorization_record)
    authorization_sha = _record_sha256(authorization_record)

    intent_id = bybit_entry_intent_id(entry_order_link_id)
    oms_record = entry_oms.get(intent_id)
    if oms_record is None:
        return _result(
            BybitDemoOperationalProtectionStatus.NO_EXECUTION_CONFIRMED,
            completed=True,
            entry_execution_confirmed=False,
            safety_mutation_performed=False,
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )
    if oms_record.client_order_id != entry_order_link_id:
        return _unresolved(
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )
    if oms_record.symbol != approval.symbol:
        return _unresolved(
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )
    if oms_record.state in _LOCAL_ZERO_FILL_STATES:
        if oms_record.filled_quantity != 0:
            return _unresolved(
                authorization_persisted=True,
                authorization_record_sha256=authorization_sha,
            )
        return _result(
            BybitDemoOperationalProtectionStatus.NO_EXECUTION_CONFIRMED,
            completed=True,
            entry_execution_confirmed=False,
            safety_mutation_performed=False,
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )

    try:
        recovery = recovery_store.load(entry_order_link_id=entry_order_link_id)
        _validate_recovery_record(approval, recovery)
        envelope = recovery.envelope
        truth = lookup_bybit_order_by_link_id(
            broker_client._signed_get,  # noqa: SLF001 - exact GET-only broker recovery primitive.
            symbol=approval.symbol,
            order_link_id=entry_order_link_id,
            expected_side=envelope.order_side,
            expected_quantity=envelope.approved_order_quantity,
        )
    except Exception:  # noqa: BLE001 - uncertainty remains explicit and blocks resubmit.
        return _unresolved(
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )

    if truth is None:
        return _unresolved(
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )
    if truth.cumulative_executed_quantity == 0:
        if truth.status not in {"Rejected", "Cancelled"}:
            return _unresolved(
                authorization_persisted=True,
                authorization_record_sha256=authorization_sha,
            )
        return _result(
            BybitDemoOperationalProtectionStatus.NO_EXECUTION_CONFIRMED,
            completed=True,
            entry_execution_confirmed=False,
            safety_mutation_performed=False,
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )
    if truth.status not in {"Filled", "Cancelled"}:
        return _unresolved(
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )

    try:
        positions = broker_client.get_positions(settle_coin="USDT")
        plan = plan_bybit_executed_entry_recovery(
            recovery,
            order_truth=truth,
            positions=positions,
        )
        recovered = execute_bybit_executed_entry_recovery(
            plan,
            client=broker_client,
        )
    except Exception:  # noqa: BLE001 - protection state cannot be guessed.
        return _unresolved(
            entry_execution_confirmed=True,
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )

    if recovered.status is BybitExecutedEntryRecoveryStatus.PROTECTED:
        return _result(
            BybitDemoOperationalProtectionStatus.RECOVERED_PROTECTED,
            completed=True,
            entry_execution_confirmed=True,
            safety_mutation_performed=True,
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )
    if recovered.status is BybitExecutedEntryRecoveryStatus.FLATTENED:
        return _result(
            BybitDemoOperationalProtectionStatus.RECOVERED_FLATTENED,
            completed=True,
            entry_execution_confirmed=True,
            safety_mutation_performed=True,
            authorization_persisted=True,
            authorization_record_sha256=authorization_sha,
        )
    return _unresolved(
        entry_execution_confirmed=True,
        authorization_persisted=True,
        authorization_record_sha256=authorization_sha,
    )


def _canonical_runtime_authorization_sha(
    approval: BybitDemoOperatorApproval,
    runtime_result: Any | None,
) -> str | None:
    if runtime_result is None:
        return None
    if getattr(runtime_result, "authorization_persisted", False) is not True:
        return None
    receipt = getattr(runtime_result, "authorization_receipt", None)
    if receipt is None:
        raise ValueError("operational reconciliation authorization receipt is missing")
    if getattr(receipt, "approval_id", None) != approval.approval_id:
        raise ValueError("operational reconciliation authorization receipt approval id mismatch")
    if (
        getattr(receipt, "entry_order_link_id", None)
        != approval.expected_entry_order_link_id
    ):
        raise ValueError("operational reconciliation authorization receipt orderLinkId mismatch")
    return _sha256(getattr(receipt, "record_sha256", None), "authorization receipt")


def _canonical_runtime_proved_protected_entry(
    approval: BybitDemoOperatorApproval,
    runtime_result: Any | None,
) -> bool:
    if runtime_result is None:
        return False
    runtime = getattr(runtime_result, "runtime_result", None)
    if runtime is None:
        return False
    if getattr(runtime_result, "authorization_persisted", False) is not True:
        return False
    if getattr(runtime, "entry_provenance_persisted", False) is not True:
        return False
    receipt = getattr(runtime, "entry_provenance_receipt", None)
    if receipt is None:
        return False
    if getattr(receipt, "entry_order_link_id", None) != approval.expected_entry_order_link_id:
        raise ValueError("operational reconciliation provenance orderLinkId mismatch")
    if getattr(runtime, "same_invocation_additional_entry_allowed", True) is not False:
        raise ValueError("operational reconciliation rejected additional-entry capability")
    if getattr(runtime, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational reconciliation rejected mainnet runtime")
    return True


def _validate_dependencies(
    *,
    authorization_store: Any,
    entry_oms: Any,
    recovery_store: Any,
    broker_client: Any,
) -> None:
    if getattr(authorization_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational reconciliation rejected mainnet authorization store")
    if getattr(authorization_store, "immutable_records", False) is not True:
        raise ValueError("operational reconciliation requires immutable authorization store")
    if not callable(getattr(authorization_store, "load", None)):
        raise ValueError("operational reconciliation authorization store requires load")

    if getattr(entry_oms, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational reconciliation rejected mainnet OMS")
    if getattr(entry_oms, "automatic_resubmit_after_submit_started_allowed", True) is not False:
        raise ValueError("operational reconciliation forbids automatic entry resubmit")
    if not callable(getattr(entry_oms, "get", None)):
        raise ValueError("operational reconciliation OMS requires get")

    if getattr(recovery_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational reconciliation rejected mainnet recovery store")
    if getattr(recovery_store, "order_writes_supported", True) is not False:
        raise ValueError("operational reconciliation recovery store cannot submit orders")
    if getattr(recovery_store, "immutable_records", False) is not True:
        raise ValueError("operational reconciliation requires immutable recovery records")
    if not callable(getattr(recovery_store, "load", None)):
        raise ValueError("operational reconciliation recovery store requires load")

    if getattr(broker_client, "environment", None) != "BYBIT_DEMO":
        raise ValueError("operational reconciliation requires BYBIT_DEMO broker client")
    if getattr(broker_client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational reconciliation rejected mainnet broker client")
    if getattr(broker_client, "protection_state_read_supported", False) is not True:
        raise ValueError("operational reconciliation requires protection-state reads")
    if not callable(getattr(broker_client, "_signed_get", None)):
        raise ValueError("operational reconciliation requires authenticated GET-only order lookup")
    if not callable(getattr(broker_client, "get_positions", None)):
        raise ValueError("operational reconciliation requires broker position reads")


def _validate_authorization_record(
    approval: BybitDemoOperatorApproval,
    record: Any,
) -> None:
    if getattr(record, "live_mainnet_order_routing_allowed", False) is True:
        raise ValueError("operational reconciliation rejected mainnet authorization record")
    authorization = getattr(record, "authorization", None)
    if authorization is None:
        raise ValueError("operational reconciliation authorization record is incomplete")
    if authorization.approval_id != approval.approval_id:
        raise ValueError("operational reconciliation authorization approval id mismatch")
    if authorization.expected_entry_order_link_id != approval.expected_entry_order_link_id:
        raise ValueError("operational reconciliation authorization orderLinkId mismatch")
    if authorization.source_snapshot_id != approval.source_snapshot_id:
        raise ValueError("operational reconciliation authorization snapshot mismatch")
    if authorization.source_evidence_rank != approval.source_evidence_rank:
        raise ValueError("operational reconciliation authorization evidence rank mismatch")
    if authorization.source_market_rank != approval.source_market_rank:
        raise ValueError("operational reconciliation authorization market rank mismatch")
    _record_sha256(record)


def _validate_recovery_record(approval: BybitDemoOperatorApproval, record: Any) -> None:
    if getattr(record, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operational reconciliation rejected mainnet recovery record")
    envelope = getattr(record, "envelope", None)
    if envelope is None:
        raise ValueError("operational reconciliation recovery envelope is missing")
    envelope.validate()
    if envelope.entry_order_link_id != approval.expected_entry_order_link_id:
        raise ValueError("operational reconciliation recovery orderLinkId mismatch")
    if envelope.trade_plan.symbol != approval.symbol:
        raise ValueError("operational reconciliation recovery symbol mismatch")
    expected_side = "Buy" if approval.side == "LONG" else "Sell"
    if envelope.order_side != expected_side:
        raise ValueError("operational reconciliation recovery side mismatch")
    if envelope.approved_order_quantity > approval.maximum_entry_quantity:
        raise ValueError("operational reconciliation recovery quantity exceeds approval")


def _record_sha256(record: Any) -> str:
    return _sha256(getattr(record, "record_sha256", None), "authorization record")


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"operational reconciliation {name} checksum is invalid")
    if any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"operational reconciliation {name} checksum is invalid")
    return value


def _result(
    status: BybitDemoOperationalProtectionStatus,
    *,
    completed: bool,
    entry_execution_confirmed: bool | None,
    safety_mutation_performed: bool,
    authorization_persisted: bool = False,
    authorization_record_sha256: str | None = None,
) -> BybitDemoOperationalProtectionReconciliation:
    result = BybitDemoOperationalProtectionReconciliation(
        status=status,
        completed=completed,
        entry_execution_confirmed=entry_execution_confirmed,
        safety_mutation_performed=safety_mutation_performed,
        authorization_persisted=authorization_persisted,
        authorization_record_sha256=authorization_record_sha256,
    )
    result.validate()
    return result


def _unresolved(
    *,
    entry_execution_confirmed: bool | None = None,
    authorization_persisted: bool = False,
    authorization_record_sha256: str | None = None,
) -> BybitDemoOperationalProtectionReconciliation:
    return _result(
        BybitDemoOperationalProtectionStatus.UNRESOLVED,
        completed=False,
        entry_execution_confirmed=entry_execution_confirmed,
        safety_mutation_performed=False,
        authorization_persisted=authorization_persisted,
        authorization_record_sha256=authorization_record_sha256,
    )


__all__ = ["reconcile_protected_bybit_demo_entry_attempt"]