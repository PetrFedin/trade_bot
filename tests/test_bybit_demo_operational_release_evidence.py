from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseStage,
    assemble_bybit_demo_operational_release_evidence,
    load_json_evidence,
)

_GIT_SHA = "a" * 40
_RUN_METADATA_SHA = "f" * 64
_SOURCE_ORDER = (
    "activation_readiness",
    "session_start",
    "supervisor",
    "arm_control",
    "operational_entry",
    "recovery_receipt",
)
_WORKFLOWS = {
    "activation_readiness": "bybit-demo-activation-readiness",
    "session_start": "bybit-demo-session-start",
    "supervisor": "bybit-demo-persistent-supervisor",
    "arm_control": "bybit-demo-control-plane",
    "operational_entry": "bybit-operator-approved-demo-execution",
    "recovery_receipt": "bybit-demo-runtime-lease-recovery",
}
_ARM_EVENT_ID = "6" * 64
_ARM_CREATED_AT = "2026-08-28T00:04:10+00:00"
_ARMED_UNTIL = "2026-08-28T00:06:10+00:00"
_ENTRY_OBSERVED_AT = "2026-08-28T00:05:10+00:00"
_RECOVERY_CREATED_AT = "2026-08-28T00:06:10+00:00"


def _sha256_json(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _activation() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "BYBIT_DEMO_ACTIVATION_READINESS_V1",
        "status": "READY_FOR_EXPLICIT_ACTIVATION_GATES",
        "passed": True,
        "reasons": [],
        "git_sha": _GIT_SHA,
        "evidence": {
            "postgres_sha256": "1" * 64,
            "connected_preflight_sha256": "2" * 64,
            "trading_credential_sha256": "3" * 64,
            "control_status_sha256": "4" * 64,
        },
        "postgres_status": "VERIFIED_READY",
        "connected_preflight_status": "READY_FOR_MANUAL_OPERATOR_APPROVAL",
        "trading_credential_status": "READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL",
        "control_mode": "HALTED",
        "ready_for_explicit_arm": True,
        "ready_for_exact_trade_approval": True,
        "operator_action_required": True,
        "arm_performed": False,
        "trade_actionable": False,
        "order_write_performed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }
    payload["manifest_sha256"] = _sha256_json(payload)
    return payload


def _session() -> dict[str, object]:
    return {
        "schema": "BYBIT_DEMO_SESSION_START_V1",
        "mode": "status",
        "status": "INITIALIZED",
        "passed": True,
        "reasons": [],
        "session_initialized": True,
        "worker_session_ready": True,
        "ledger_revision_sha256": "5" * 64,
        "outcome_count": 0,
        "opening_equity_positive": True,
        "preflight_record_sha256": None,
        "git_sha": _GIT_SHA,
        "session_start_id": None,
        "fixed_egress_required": True,
        "explicit_operator_action_required": True,
        "automatic_reset_allowed": False,
        "trading_credential_required": False,
        "order_write_performed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _supervisor() -> dict[str, object]:
    return {
        "schema": "BYBIT_DEMO_PERSISTENT_SUPERVISOR_V1",
        "git_sha": _GIT_SHA,
        "status": "IDLE_NO_ACTIVE_TRADE",
        "blocked": False,
        "reasons": [],
        "active_symbol": None,
        "runtime_status": None,
        "session_risk_action": None,
        "session_high_water_advanced": None,
        "reconciled_terminal_outcome_count": None,
        "new_entry_attempted": False,
        "autonomous_entry_allowed": False,
        "operator_approval_bypass_allowed": False,
        "same_invocation_additional_entry_allowed": False,
        "demo_only": True,
        "live_mainnet_order_routing_allowed": False,
    }


def _arm_control() -> dict[str, object]:
    return {
        "schema": "BYBIT_DEMO_CONTROL_OPERATION_V1",
        "git_sha": _GIT_SHA,
        "mode": "arm",
        "status": "ARMED",
        "passed": True,
        "fixed_egress_required": True,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
        "preflight": {
            "schema": "BYBIT_DEMO_CONNECTED_PREFLIGHT_V1",
            "status": "READY_FOR_MANUAL_OPERATOR_APPROVAL",
        },
        "receipt": {
            "schema": "BYBIT_DEMO_CONTROL_EVENT_RECEIPT_V1",
            "event_id": _ARM_EVENT_ID,
            "event_kind": "ARM_NEW_ENTRIES",
            "created_at": _ARM_CREATED_AT,
            "armed_until": _ARMED_UNTIL,
            "preflight_record_sha256": "d" * 64,
            "immutable_record": True,
            "order_submission_supported": False,
            "live_mainnet_order_routing_allowed": False,
        },
        "decision": {
            "schema": "BYBIT_DEMO_CONTROL_DECISION_V1",
            "mode": "ARMED_NEW_ENTRIES",
            "reasons": [],
            "new_entry_allowed": True,
            "latest_event_id": _ARM_EVENT_ID,
            "latest_event_kind": "ARM_NEW_ENTRIES",
            "armed_until": _ARMED_UNTIL,
            "immutable_audit": True,
            "order_writes_supported": False,
            "live_mainnet_order_routing_allowed": False,
        },
    }


def _entry() -> dict[str, object]:
    return {
        "schema": "BYBIT_DEMO_OPERATIONAL_ENTRY_EVIDENCE_V1",
        "git_sha": _GIT_SHA,
        "status": "ENTRY_CYCLE_COMPLETE",
        "observed_at": _ENTRY_OBSERVED_AT,
        "approval_id": "approval-a",
        "source_snapshot_id": "snapshot-a",
        "source_evidence_rank": 1,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_order_link_id": "entry-a",
        "pinned_control_event_id": _ARM_EVENT_ID,
        "pinned_control_armed_until": _ARMED_UNTIL,
        "runtime_status": "ENTRY_CYCLE_EXECUTED",
        "runtime_error_type": None,
        "authorization_persisted": True,
        "authorization_record_sha256": "7" * 64,
        "entry_provenance_persisted": True,
        "entry_provenance_record_sha256": "8" * 64,
        "protection_reconciliation_status": "CANONICAL_RUNTIME_RECONCILED",
        "protection_reconciliation_completed": True,
        "same_invocation_additional_entry_allowed": False,
        "fixed_egress_verified": True,
        "protected_dispatch_required": True,
        "automatic_arm_allowed": False,
        "ranked_fallback_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _recovery() -> dict[str, object]:
    return {
        "schema": "BYBIT_DEMO_RUNTIME_LEASE_RECOVERY_RECEIPT_V1",
        "git_sha": _GIT_SHA,
        "status": "RECOVERED",
        "recovery_id": "9" * 64,
        "lease_owner_sha256": "a" * 64,
        "control_event_id": "b" * 64,
        "active_checkpoint_present": True,
        "created_at": _RECOVERY_CREATED_AT,
        "idempotent_existing_recovery": False,
        "immutable_audit": True,
        "automatic_recovery_allowed": False,
        "automatic_stale_takeover_allowed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _payloads() -> dict[str, dict[str, object]]:
    return {
        "activation_readiness": _activation(),
        "session_start": _session(),
        "supervisor": _supervisor(),
        "arm_control": _arm_control(),
        "operational_entry": _entry(),
        "recovery_receipt": _recovery(),
    }


def _hashes(count: int) -> dict[str, str]:
    return {
        name: f"{index + 1:x}" * 64
        for index, name in enumerate(_SOURCE_ORDER[:count])
    }


def _run_metadata(count: int) -> dict[str, dict[str, object]]:
    start = datetime(2026, 8, 28, 0, 1, tzinfo=UTC)
    return {
        name: {
            "run_id": 1000 + index,
            "workflow_name": _WORKFLOWS[name],
            "event": "workflow_dispatch",
            "conclusion": "success",
            "head_sha": _GIT_SHA,
            "run_started_at": (start + timedelta(minutes=index)).isoformat(),
            "run_completed_at": (
                start + timedelta(minutes=index, seconds=30)
            ).isoformat(),
        }
        for index, name in enumerate(_SOURCE_ORDER[:count])
    }


def _assemble(count: int, **overrides: object):
    payloads = _payloads()
    kwargs: dict[str, object] = {
        "git_sha": _GIT_SHA,
        "activation_readiness": payloads["activation_readiness"],
        "evidence_sha256": _hashes(count),
        "source_run_metadata": _run_metadata(count),
        "source_run_metadata_sha256": _RUN_METADATA_SHA,
        "session_start": payloads["session_start"] if count >= 2 else None,
        "supervisor": payloads["supervisor"] if count >= 3 else None,
        "arm_control": payloads["arm_control"] if count >= 4 else None,
        "operational_entry": payloads["operational_entry"] if count >= 5 else None,
        "recovery_receipt": payloads["recovery_receipt"] if count >= 6 else None,
    }
    kwargs.update(overrides)
    return assemble_bybit_demo_operational_release_evidence(**kwargs)


@pytest.mark.parametrize(
    ("count", "stage", "next_required"),
    [
        (1, BybitDemoOperationalReleaseStage.INFRA_READY, "session_start"),
        (2, BybitDemoOperationalReleaseStage.SESSION_READY, "supervisor"),
        (3, BybitDemoOperationalReleaseStage.SUPERVISOR_READY, "arm_control"),
        (4, BybitDemoOperationalReleaseStage.ARM_PROVEN, "operational_entry"),
        (5, BybitDemoOperationalReleaseStage.DEMO_ENTRY_PROVEN, "recovery_receipt"),
        (6, BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN, None),
    ],
)
def test_release_evidence_reports_highest_proven_stage(
    count: int,
    stage: BybitDemoOperationalReleaseStage,
    next_required: str | None,
) -> None:
    result = _assemble(count)

    assert result.stage is stage
    assert result.passed is True
    assert result.next_required_evidence == next_required
    assert result.release_gate_complete is (count == 6)
    assert result.automatic_activation_allowed is False
    assert result.order_write_performed is False
    assert result.order_writes_supported is False
    assert result.live_mainnet_order_routing_allowed is False
    assert tuple(result.source_runs) == _SOURCE_ORDER[:count]


def test_full_manifest_is_self_hashing_and_run_bound() -> None:
    payload = _assemble(6).to_payload()
    manifest_sha = payload.pop("manifest_sha256")

    assert manifest_sha == _sha256_json(payload)
    assert payload["source_run_metadata_sha256"] == _RUN_METADATA_SHA
    assert payload["source_runs"]["arm_control"]["run_id"] == 1003
    assert payload["source_runs"]["operational_entry"]["run_id"] == 1004
    assert payload["release_gate_complete"] is True


def test_tampered_activation_manifest_fails_closed() -> None:
    activation = _activation()
    activation["control_mode"] = "ARMED"

    result = _assemble(1, activation_readiness=activation)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "ACTIVATION_READINESS_MANIFEST_SHA_MISMATCH" in result.reasons


def test_mismatched_artifact_git_sha_fails_closed() -> None:
    supervisor = _supervisor()
    supervisor["git_sha"] = "b" * 40

    result = _assemble(3, supervisor=supervisor)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "SUPERVISOR_GIT_SHA_MISMATCH" in result.reasons


def test_non_contiguous_artifacts_fail_closed() -> None:
    payloads = _payloads()
    metadata = {
        key: value
        for key, value in _run_metadata(3).items()
        if key != "session_start"
    }

    result = assemble_bybit_demo_operational_release_evidence(
        git_sha=_GIT_SHA,
        activation_readiness=payloads["activation_readiness"],
        session_start=None,
        supervisor=payloads["supervisor"],
        evidence_sha256={
            "activation_readiness": "1" * 64,
            "supervisor": "3" * 64,
        },
        source_run_metadata=metadata,
        source_run_metadata_sha256=_RUN_METADATA_SHA,
    )

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "NON_CONTIGUOUS_EVIDENCE_CHAIN:supervisor" in result.reasons


def test_wrong_source_workflow_or_run_order_fails_closed() -> None:
    metadata = _run_metadata(2)
    metadata["session_start"]["workflow_name"] = "unrelated-workflow"
    metadata["session_start"]["run_started_at"] = "2026-08-28T00:01:20+00:00"

    result = _assemble(2, source_run_metadata=metadata)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "SOURCE_RUN_WORKFLOW_MISMATCH:session_start" in result.reasons
    assert "SOURCE_RUN_ORDER_INVALID:session_start" in result.reasons


def test_non_manual_or_wrong_head_source_run_fails_closed() -> None:
    metadata = _run_metadata(1)
    metadata["activation_readiness"]["event"] = "pull_request"
    metadata["activation_readiness"]["head_sha"] = "b" * 40

    result = _assemble(1, source_run_metadata=metadata)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "SOURCE_RUN_NOT_MANUAL_DISPATCH:activation_readiness" in result.reasons
    assert "SOURCE_RUN_GIT_SHA_MISMATCH:activation_readiness" in result.reasons


def test_arm_receipt_must_be_successful_exact_head_evidence() -> None:
    arm = _arm_control()
    arm["status"] = "HALTED"
    arm["passed"] = False
    arm["git_sha"] = "b" * 40

    result = _assemble(4, arm_control=arm)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "ARM_CONTROL_NOT_ARMED" in result.reasons
    assert "ARM_CONTROL_GIT_SHA_MISMATCH" in result.reasons


def test_arm_receipt_identity_must_match_entry_pin() -> None:
    entry = _entry()
    entry["pinned_control_event_id"] = "e" * 64

    result = _assemble(5, operational_entry=entry)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "OPERATIONAL_ENTRY_ARM_EVENT_MISMATCH" in result.reasons


def test_entry_must_remain_inside_exact_arm_window() -> None:
    entry = _entry()
    entry["observed_at"] = _ARMED_UNTIL

    result = _assemble(5, operational_entry=entry)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "OPERATIONAL_ENTRY_OUTSIDE_ARM_WINDOW" in result.reasons


def test_arm_event_timestamp_must_belong_to_arm_run() -> None:
    arm = _arm_control()
    receipt = dict(arm["receipt"])
    receipt["created_at"] = "2026-08-28T00:03:50+00:00"
    arm["receipt"] = receipt

    result = _assemble(4, arm_control=arm)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "ARM_CONTROL_OUTSIDE_SOURCE_RUN_WINDOW" in result.reasons


def test_unresolved_or_replacement_entry_fails_closed() -> None:
    entry = _entry()
    entry["protection_reconciliation_status"] = "UNRESOLVED"
    entry["protection_reconciliation_completed"] = False
    entry["same_invocation_additional_entry_allowed"] = True

    result = _assemble(5, operational_entry=entry)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "OPERATIONAL_ENTRY_EXECUTION_NOT_SAFELY_RECONCILED" in result.reasons
    assert "OPERATIONAL_ENTRY_RECONCILIATION_INCOMPLETE" in result.reasons
    assert "OPERATIONAL_ENTRY_REPLACEMENT_ALLOWED" in result.reasons


def test_recovery_inspection_cannot_substitute_for_recovery_receipt() -> None:
    recovery = _recovery()
    recovery["schema"] = "BYBIT_DEMO_RUNTIME_LEASE_RECOVERY_INSPECTION_V1"
    recovery["status"] = "RECOVERY_REQUIRED"

    result = _assemble(6, recovery_receipt=recovery)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "RECOVERY_RECEIPT_SCHEMA_INVALID" in result.reasons
    assert "RECOVERY_DRILL_NOT_NEWLY_PROVEN" in result.reasons


def test_idempotent_old_recovery_cannot_substitute_for_new_drill() -> None:
    recovery = _recovery()
    recovery["status"] = "ALREADY_RECOVERED"
    recovery["idempotent_existing_recovery"] = True

    result = _assemble(6, recovery_receipt=recovery)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "RECOVERY_DRILL_NOT_NEWLY_PROVEN" in result.reasons
    assert "RECOVERY_RECEIPT_IDEMPOTENCY_INVALID" in result.reasons


def test_recovery_receipt_must_be_created_after_operational_entry() -> None:
    recovery = _recovery()
    recovery["created_at"] = "2026-08-28T00:05:00+00:00"

    result = _assemble(6, recovery_receipt=recovery)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "RECOVERY_RECEIPT_NOT_AFTER_OPERATIONAL_ENTRY" in result.reasons
    assert "RECOVERY_RECEIPT_OUTSIDE_SOURCE_RUN_WINDOW" in result.reasons


def test_invalid_entry_timestamp_blocks_recovery_linkage() -> None:
    entry = _entry()
    entry["observed_at"] = "not-a-timestamp"

    result = _assemble(6, operational_entry=entry)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "OPERATIONAL_ENTRY_ARM_TEMPORAL_LINK_INVALID" in result.reasons
    assert "OPERATIONAL_ENTRY_SOURCE_RUN_TIME_BINDING_INVALID" in result.reasons


def test_evidence_hash_without_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="hash was supplied without evidence"):
        _assemble(
            1,
            evidence_sha256={
                "activation_readiness": "1" * 64,
                "session_start": "2" * 64,
            },
        )


def test_load_json_evidence_hashes_raw_bytes(tmp_path: Path) -> None:
    target = tmp_path / "evidence.json"
    raw = b'{"a":1}\n'
    target.write_bytes(raw)

    payload, digest = load_json_evidence(target)

    assert payload == {"a": 1}
    assert digest == hashlib.sha256(raw).hexdigest()


def test_duplicate_source_run_id_is_rejected() -> None:
    metadata = deepcopy(_run_metadata(2))
    metadata["session_start"]["run_id"] = metadata["activation_readiness"]["run_id"]

    result = _assemble(2, source_run_metadata=metadata)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "SOURCE_RUN_ID_REUSED:session_start" in result.reasons
