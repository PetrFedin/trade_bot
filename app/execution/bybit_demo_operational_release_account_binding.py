from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.execution.bybit_demo_operational_release_checkpoint_binding import (
    assemble_checkpoint_bound_bybit_demo_operational_release_evidence,
)
from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseEvidence,
    BybitDemoOperationalReleaseStage,
)


def assemble_account_bound_bybit_demo_operational_release_evidence(
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
    """Require same-account proof at every credential-bearing release stage."""

    result = assemble_checkpoint_bound_bybit_demo_operational_release_evidence(
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

    reasons: list[str] = []
    if activation_readiness.get("demo_account_identity_verified") is not True:
        reasons.append("ACTIVATION_READINESS_ACCOUNT_IDENTITY_NOT_VERIFIED")
    if supervisor is not None and supervisor.get("demo_account_identity_verified") is not True:
        reasons.append("SUPERVISOR_ACCOUNT_IDENTITY_NOT_VERIFIED")
    if (
        operational_entry is not None
        and operational_entry.get("demo_account_identity_verified") is not True
    ):
        reasons.append("OPERATIONAL_ENTRY_ACCOUNT_IDENTITY_NOT_VERIFIED")
    if not reasons:
        return result
    return BybitDemoOperationalReleaseEvidence(
        stage=BybitDemoOperationalReleaseStage.BLOCKED,
        reasons=(*result.reasons, *reasons),
        git_sha=result.git_sha,
        evidence_sha256=result.evidence_sha256,
        source_runs=result.source_runs,
        source_run_metadata_sha256=result.source_run_metadata_sha256,
        next_required_evidence=None,
        release_gate_complete=False,
    )


__all__ = ["assemble_account_bound_bybit_demo_operational_release_evidence"]
