from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseEvidence,
    BybitDemoOperationalReleaseStage,
    assemble_bybit_demo_operational_release_evidence,
)


def assemble_checkpoint_bound_bybit_demo_operational_release_evidence(
    *,
    git_sha: str,
    activation_readiness: Mapping[str, Any],
    evidence_sha256: Mapping[str, str],
    source_run_metadata: Mapping[str, Any],
    source_run_metadata_sha256: str,
    session_start: Mapping[str, Any] | None = None,
    supervisor: Mapping[str, Any] | None = None,
    arm_control: Mapping[str, Any] | None = None,
    operational_entry: Mapping[str, Any] | None = None,
    halt_control: Mapping[str, Any] | None = None,
    recovery_receipt: Mapping[str, Any] | None = None,
) -> BybitDemoOperationalReleaseEvidence:
    """Add exact entry-checkpoint lineage to the read-only operational release gate.

    v123 recovery legitimately supports orphaned runtime leases with or without an active
    checkpoint. A *full entry recovery drill*, however, is stronger: the immutable v123 audit must
    prove that the recovered active checkpoint was the checkpoint created for the exact approved
    entry represented by this release chain.
    """

    result = assemble_bybit_demo_operational_release_evidence(
        git_sha=git_sha,
        activation_readiness=activation_readiness,
        evidence_sha256=evidence_sha256,
        source_run_metadata=source_run_metadata,
        source_run_metadata_sha256=source_run_metadata_sha256,
        session_start=session_start,
        supervisor=supervisor,
        arm_control=arm_control,
        operational_entry=operational_entry,
        halt_control=halt_control,
        recovery_receipt=recovery_receipt,
    )
    if result.stage is BybitDemoOperationalReleaseStage.BLOCKED:
        return result
    if recovery_receipt is None:
        return result
    if operational_entry is None:
        return _blocked_from(result, "RECOVERY_DRILL_ENTRY_EVIDENCE_MISSING")

    reason = _checkpoint_binding_failure_reason(
        operational_entry=operational_entry,
        recovery_receipt=recovery_receipt,
    )
    if reason is None:
        return result
    return _blocked_from(result, reason)


def _checkpoint_binding_failure_reason(
    *,
    operational_entry: Mapping[str, Any],
    recovery_receipt: Mapping[str, Any],
) -> str | None:
    entry_order_link_id = operational_entry.get("entry_order_link_id")
    if not isinstance(entry_order_link_id, str) or not entry_order_link_id.strip():
        return "OPERATIONAL_ENTRY_ORDER_LINK_ID_INVALID"
    if entry_order_link_id != entry_order_link_id.strip():
        return "OPERATIONAL_ENTRY_ORDER_LINK_ID_INVALID"

    if recovery_receipt.get("active_checkpoint_present") is not True:
        return "RECOVERY_DRILL_ACTIVE_CHECKPOINT_NOT_PROVEN"

    checkpoint_sha = recovery_receipt.get(
        "active_checkpoint_entry_order_link_id_sha256"
    )
    if not _is_sha256(checkpoint_sha):
        return "RECOVERY_DRILL_CHECKPOINT_IDENTITY_INVALID"

    expected_sha = hashlib.sha256(entry_order_link_id.encode("utf-8")).hexdigest()
    if checkpoint_sha != expected_sha:
        return "RECOVERY_DRILL_ENTRY_CHECKPOINT_MISMATCH"
    return None


def _blocked_from(
    result: BybitDemoOperationalReleaseEvidence,
    reason: str,
) -> BybitDemoOperationalReleaseEvidence:
    return BybitDemoOperationalReleaseEvidence(
        stage=BybitDemoOperationalReleaseStage.BLOCKED,
        reasons=(*result.reasons, reason),
        git_sha=result.git_sha,
        evidence_sha256=result.evidence_sha256,
        source_runs=result.source_runs,
        source_run_metadata_sha256=result.source_run_metadata_sha256,
        next_required_evidence=None,
        release_gate_complete=False,
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = ["assemble_checkpoint_bound_bybit_demo_operational_release_evidence"]
