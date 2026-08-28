from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.execution.bybit_demo_operational_release_account_binding import (
    assemble_account_bound_bybit_demo_operational_release_evidence,
)
from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseEvidence,
    BybitDemoOperationalReleaseStage,
)

_SOURCE_ORDER = (
    "activation_readiness",
    "session_start",
    "supervisor",
    "arm_control",
    "operational_entry",
    "halt_control",
    "recovery_receipt",
)
_ACCOUNT_BOUND_SOURCES = frozenset(
    {
        "activation_readiness",
        "supervisor",
        "arm_control",
        "operational_entry",
        "halt_control",
    }
)


@dataclass(frozen=True)
class BybitDemoOperationalZoneBoundReleaseEvidence:
    base: BybitDemoOperationalReleaseEvidence
    zone_binding_sha256: Mapping[str, str]
    operational_zone_binding_verified: bool

    @property
    def stage(self) -> BybitDemoOperationalReleaseStage:
        return self.base.stage

    def to_payload(self) -> dict[str, Any]:
        payload = self.base.to_payload()
        payload.pop("manifest_sha256", None)
        payload["operational_zone_binding_verified"] = self.operational_zone_binding_verified
        payload["zone_binding_sha256"] = dict(sorted(self.zone_binding_sha256.items()))
        payload["manifest_sha256"] = _sha256_canonical_json(payload)
        return payload


def assemble_zone_bound_bybit_demo_operational_release_evidence(
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
    base = assemble_account_bound_bybit_demo_operational_release_evidence(
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
    if base.stage is BybitDemoOperationalReleaseStage.BLOCKED:
        return BybitDemoOperationalZoneBoundReleaseEvidence(
            base=base,
            zone_binding_sha256=dict(zone_binding_sha256),
            operational_zone_binding_verified=False,
        )

    supplied_names = _supplied_names(
        session_start=session_start,
        supervisor=supervisor,
        arm_control=arm_control,
        operational_entry=operational_entry,
        halt_control=halt_control,
        recovery_receipt=recovery_receipt,
    )
    reasons = _validate_zone_bindings(
        git_sha=git_sha,
        supplied_names=supplied_names,
        zone_bindings=zone_bindings,
        zone_binding_sha256=zone_binding_sha256,
        source_run_metadata=source_run_metadata,
    )
    if not reasons:
        return BybitDemoOperationalZoneBoundReleaseEvidence(
            base=base,
            zone_binding_sha256=dict(zone_binding_sha256),
            operational_zone_binding_verified=True,
        )

    blocked = BybitDemoOperationalReleaseEvidence(
        stage=BybitDemoOperationalReleaseStage.BLOCKED,
        reasons=(*base.reasons, *reasons),
        git_sha=base.git_sha,
        evidence_sha256=base.evidence_sha256,
        source_runs=base.source_runs,
        source_run_metadata_sha256=base.source_run_metadata_sha256,
        next_required_evidence=None,
        release_gate_complete=False,
    )
    return BybitDemoOperationalZoneBoundReleaseEvidence(
        base=blocked,
        zone_binding_sha256=dict(zone_binding_sha256),
        operational_zone_binding_verified=False,
    )


def _supplied_names(
    *,
    session_start: Mapping[str, Any] | None,
    supervisor: Mapping[str, Any] | None,
    arm_control: Mapping[str, Any] | None,
    operational_entry: Mapping[str, Any] | None,
    halt_control: Mapping[str, Any] | None,
    recovery_receipt: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    supplied = {
        "activation_readiness": True,
        "session_start": session_start is not None,
        "supervisor": supervisor is not None,
        "arm_control": arm_control is not None,
        "operational_entry": operational_entry is not None,
        "halt_control": halt_control is not None,
        "recovery_receipt": recovery_receipt is not None,
    }
    return tuple(name for name in _SOURCE_ORDER if supplied[name])


def _validate_zone_bindings(
    *,
    git_sha: str,
    supplied_names: tuple[str, ...],
    zone_bindings: Mapping[str, Mapping[str, Any]],
    zone_binding_sha256: Mapping[str, str],
    source_run_metadata: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    expected = set(supplied_names)
    binding_names = set(zone_bindings)
    hash_names = set(zone_binding_sha256)
    for missing in sorted(expected - binding_names):
        reasons.append(f"OPERATIONAL_ZONE_BINDING_MISSING:{missing}")
    for extra in sorted(binding_names - expected):
        reasons.append(f"OPERATIONAL_ZONE_BINDING_WITHOUT_EVIDENCE:{extra}")
    for missing in sorted(expected - hash_names):
        reasons.append(f"OPERATIONAL_ZONE_BINDING_HASH_MISSING:{missing}")
    for extra in sorted(hash_names - expected):
        reasons.append(f"OPERATIONAL_ZONE_BINDING_HASH_WITHOUT_EVIDENCE:{extra}")
    if reasons:
        return reasons

    key_markers: set[str] = set()
    database_tokens: set[str] = set()
    account_tokens: set[str] = set()
    for name in supplied_names:
        payload = zone_bindings[name]
        digest = zone_binding_sha256[name]
        if not _is_sha256(digest):
            reasons.append(f"OPERATIONAL_ZONE_BINDING_HASH_INVALID:{name}")
        if payload.get("schema") != "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V1":
            reasons.append(f"OPERATIONAL_ZONE_BINDING_SCHEMA_INVALID:{name}")
        if payload.get("status") != "BOUND" or payload.get("passed") is not True:
            reasons.append(f"OPERATIONAL_ZONE_BINDING_NOT_BOUND:{name}")
        if payload.get("producer") != name:
            reasons.append(f"OPERATIONAL_ZONE_BINDING_PRODUCER_MISMATCH:{name}")
        if payload.get("git_sha") != git_sha:
            reasons.append(f"OPERATIONAL_ZONE_BINDING_GIT_SHA_MISMATCH:{name}")
        if payload.get("binding_algorithm") != "HMAC-SHA256":
            reasons.append(f"OPERATIONAL_ZONE_BINDING_ALGORITHM_INVALID:{name}")
        if payload.get("order_writes_supported") is not False:
            reasons.append(f"OPERATIONAL_ZONE_BINDING_UNSAFE_ORDER_CAPABILITY:{name}")
        if payload.get("live_mainnet_order_routing_allowed") is not False:
            reasons.append(f"OPERATIONAL_ZONE_BINDING_UNSAFE_MAINNET_CAPABILITY:{name}")

        marker = payload.get("binding_key_marker_sha256")
        if not _is_sha256(marker):
            reasons.append(f"OPERATIONAL_ZONE_BINDING_KEY_MARKER_INVALID:{name}")
        else:
            key_markers.add(marker)

        database_token = payload.get("database_binding_sha256")
        if payload.get("database_binding_present") is not True or not _is_sha256(
            database_token
        ):
            reasons.append(f"OPERATIONAL_ZONE_DATABASE_BINDING_INVALID:{name}")
        else:
            database_tokens.add(database_token)

        account_required = name in _ACCOUNT_BOUND_SOURCES
        account_present = payload.get("demo_account_binding_present")
        account_token = payload.get("demo_account_binding_sha256")
        if account_required:
            if account_present is not True or not _is_sha256(account_token):
                reasons.append(f"OPERATIONAL_ZONE_ACCOUNT_BINDING_INVALID:{name}")
            else:
                account_tokens.add(account_token)
        elif account_present is not False or account_token is not None:
            reasons.append(f"OPERATIONAL_ZONE_UNEXPECTED_ACCOUNT_BINDING:{name}")

        _validate_binding_time(
            name=name,
            observed_at=payload.get("observed_at"),
            source_run=source_run_metadata.get(name),
            reasons=reasons,
        )

    if len(key_markers) > 1:
        reasons.append("OPERATIONAL_ZONE_BINDING_SECRET_DRIFT")
    if len(database_tokens) > 1:
        reasons.append("OPERATIONAL_ZONE_DATABASE_DRIFT")
    if len(account_tokens) > 1:
        reasons.append("OPERATIONAL_ZONE_DEMO_ACCOUNT_DRIFT")
    return reasons


def _validate_binding_time(
    *,
    name: str,
    observed_at: Any,
    source_run: Any,
    reasons: list[str],
) -> None:
    if not isinstance(source_run, Mapping):
        reasons.append(f"OPERATIONAL_ZONE_SOURCE_RUN_MISSING:{name}")
        return
    try:
        observed = _utc_datetime(observed_at)
        started = _utc_datetime(source_run.get("run_started_at"))
        completed = _utc_datetime(source_run.get("run_completed_at"))
    except ValueError:
        reasons.append(f"OPERATIONAL_ZONE_SOURCE_RUN_TIME_INVALID:{name}")
        return
    if observed < started or observed > completed:
        reasons.append(f"OPERATIONAL_ZONE_BINDING_OUTSIDE_SOURCE_RUN:{name}")


def _utc_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp is required")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("timestamp is invalid") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return moment.astimezone(UTC)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_canonical_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BybitDemoOperationalZoneBoundReleaseEvidence",
    "assemble_zone_bound_bybit_demo_operational_release_evidence",
]
