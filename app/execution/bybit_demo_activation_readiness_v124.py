from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from app.execution.bybit_demo_activation_readiness import (
    BybitDemoActivationReadinessResult,
    BybitDemoActivationReadinessStatus,
    assemble_bybit_demo_activation_readiness,
)


def assemble_v124_bybit_demo_activation_readiness(
    *,
    git_sha: str,
    postgres_payload: Mapping[str, Any],
    connected_preflight_payload: Mapping[str, Any],
    trading_credential_payload: Mapping[str, Any],
    same_account_payload: Mapping[str, Any],
    control_status_payload: Mapping[str, Any],
    evidence_sha256: Mapping[str, str],
) -> BybitDemoActivationReadinessResult:
    reasons: list[str] = []
    if postgres_payload.get("schema") != "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V4":
        reasons.append("POSTGRES_V124_EVIDENCE_SCHEMA_INVALID")
    if postgres_payload.get("logical_database_identity_verified") is not True:
        reasons.append("POSTGRES_LOGICAL_DATABASE_IDENTITY_NOT_VERIFIED")

    normalized_postgres = dict(postgres_payload)
    normalized_postgres["schema"] = "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V3"
    normalized_postgres.pop("logical_database_identity_verified", None)
    base = assemble_bybit_demo_activation_readiness(
        git_sha=git_sha,
        postgres_payload=normalized_postgres,
        connected_preflight_payload=connected_preflight_payload,
        trading_credential_payload=trading_credential_payload,
        same_account_payload=same_account_payload,
        control_status_payload=control_status_payload,
        evidence_sha256=evidence_sha256,
    )
    if not reasons:
        return base
    return replace(
        base,
        status=BybitDemoActivationReadinessStatus.BLOCKED,
        reasons=(*base.reasons, *reasons),
        ready_for_explicit_arm=False,
        ready_for_exact_trade_approval=False,
    )


__all__ = ["assemble_v124_bybit_demo_activation_readiness"]
