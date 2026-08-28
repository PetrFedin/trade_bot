from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseEvidence,
    BybitDemoOperationalReleaseStage,
)
from app.execution.bybit_demo_operational_release_zone_binding import (
    BybitDemoOperationalZoneBoundReleaseEvidence,
    assemble_zone_bound_bybit_demo_operational_release_evidence,
)


def assemble_logical_db_bound_bybit_demo_operational_release_evidence(
    *,
    git_sha: str,
    activation_readiness: Mapping[str, Any],
    evidence_sha256: Mapping[str, str],
    source_run_metadata: Mapping[str, Any],
    source_run_metadata_sha256: str,
    zone_bindings: Mapping[str, Mapping[str, Any]],
    zone_binding_sha256: Mapping[str, str],
    session_start: Mapping[str, Any] | None = None,
    supervisor: Mapping[str, Any] | None = None,
    arm_control: Mapping[str, Any] | None = None,
    operational_entry: Mapping[str, Any] | None = None,
    halt_control: Mapping[str, Any] | None = None,
    recovery_receipt: Mapping[str, Any] | None = None,
) -> BybitDemoOperationalZoneBoundReleaseEvidence:
    reasons: list[str] = []
    legacy_bindings: dict[str, dict[str, Any]] = {}
    for name, payload in zone_bindings.items():
        if payload.get("schema") != "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V2":
            reasons.append(f"OPERATIONAL_ZONE_V124_SCHEMA_INVALID:{name}")
        if payload.get("logical_database_identity_verified") is not True:
            reasons.append(f"OPERATIONAL_ZONE_LOGICAL_DB_IDENTITY_MISSING:{name}")
        normalized = dict(payload)
        normalized["schema"] = "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V1"
        normalized.pop("logical_database_identity_verified", None)
        legacy_bindings[name] = normalized

    result = assemble_zone_bound_bybit_demo_operational_release_evidence(
        git_sha=git_sha,
        activation_readiness=activation_readiness,
        evidence_sha256=evidence_sha256,
        source_run_metadata=source_run_metadata,
        source_run_metadata_sha256=source_run_metadata_sha256,
        zone_bindings=legacy_bindings,
        zone_binding_sha256=zone_binding_sha256,
        session_start=session_start,
        supervisor=supervisor,
        arm_control=arm_control,
        operational_entry=operational_entry,
        halt_control=halt_control,
        recovery_receipt=recovery_receipt,
    )
    if not reasons or result.stage is BybitDemoOperationalReleaseStage.BLOCKED:
        if not reasons:
            return result
        if result.stage is BybitDemoOperationalReleaseStage.BLOCKED:
            blocked = replace(
                result.base,
                reasons=(*result.base.reasons, *reasons),
                release_gate_complete=False,
            )
            return replace(
                result,
                base=blocked,
                operational_zone_binding_verified=False,
            )

    blocked_base = BybitDemoOperationalReleaseEvidence(
        stage=BybitDemoOperationalReleaseStage.BLOCKED,
        reasons=(*result.base.reasons, *reasons),
        git_sha=result.base.git_sha,
        evidence_sha256=result.base.evidence_sha256,
        source_runs=result.base.source_runs,
        source_run_metadata_sha256=result.base.source_run_metadata_sha256,
        next_required_evidence=None,
        release_gate_complete=False,
    )
    return BybitDemoOperationalZoneBoundReleaseEvidence(
        base=blocked_base,
        zone_binding_sha256=result.zone_binding_sha256,
        operational_zone_binding_verified=False,
    )


__all__ = ["assemble_logical_db_bound_bybit_demo_operational_release_evidence"]
