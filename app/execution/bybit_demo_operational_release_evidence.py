from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

_SOURCE_ORDER = (
    "activation_readiness",
    "session_start",
    "supervisor",
    "operational_entry",
    "recovery_receipt",
)
_WORKFLOW_NAMES = {
    "activation_readiness": "bybit-demo-activation-readiness",
    "session_start": "bybit-demo-session-start",
    "supervisor": "bybit-demo-persistent-supervisor",
    "operational_entry": "bybit-operator-approved-demo-execution",
    "recovery_receipt": "bybit-demo-runtime-lease-recovery",
}


class BybitDemoOperationalReleaseStage(StrEnum):
    BLOCKED = "BLOCKED"
    INFRA_READY = "INFRA_READY"
    SESSION_READY = "SESSION_READY"
    SUPERVISOR_READY = "SUPERVISOR_READY"
    DEMO_ENTRY_PROVEN = "DEMO_ENTRY_PROVEN"
    RECOVERY_DRILL_PROVEN = "RECOVERY_DRILL_PROVEN"


@dataclass(frozen=True)
class BybitDemoOperationalReleaseEvidence:
    stage: BybitDemoOperationalReleaseStage
    reasons: tuple[str, ...]
    git_sha: str
    evidence_sha256: Mapping[str, str]
    source_runs: Mapping[str, Mapping[str, Any]]
    source_run_metadata_sha256: str
    next_required_evidence: str | None
    release_gate_complete: bool
    operator_action_required: bool = True
    automatic_activation_allowed: bool = False
    order_write_performed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    @property
    def passed(self) -> bool:
        return self.stage is not BybitDemoOperationalReleaseStage.BLOCKED

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "BYBIT_DEMO_OPERATIONAL_RELEASE_EVIDENCE_V1",
            "stage": self.stage.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "git_sha": self.git_sha,
            "evidence_sha256": dict(sorted(self.evidence_sha256.items())),
            "source_runs": {
                name: dict(value)
                for name, value in sorted(self.source_runs.items())
            },
            "source_run_metadata_sha256": self.source_run_metadata_sha256,
            "next_required_evidence": self.next_required_evidence,
            "release_gate_complete": self.release_gate_complete,
            "operator_action_required": self.operator_action_required,
            "automatic_activation_allowed": self.automatic_activation_allowed,
            "order_write_performed": self.order_write_performed,
            "order_writes_supported": self.order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }
        payload["manifest_sha256"] = _sha256_canonical_json(payload)
        return payload


def assemble_bybit_demo_operational_release_evidence(
    *,
    git_sha: str,
    activation_readiness: Mapping[str, Any],
    evidence_sha256: Mapping[str, str],
    source_run_metadata: Mapping[str, Any],
    source_run_metadata_sha256: str,
    session_start: Mapping[str, Any] | None = None,
    supervisor: Mapping[str, Any] | None = None,
    operational_entry: Mapping[str, Any] | None = None,
    recovery_receipt: Mapping[str, Any] | None = None,
) -> BybitDemoOperationalReleaseEvidence:
    """Assemble one read-only, exact-head operational evidence chain.

    Missing later-stage evidence produces the highest proven stage. Any supplied artifact or run
    metadata that is malformed, unsafe, out of sequence or bound to a different Git SHA fails the
    whole manifest closed.
    """

    _validate_git_sha(git_sha)
    _validate_sha256(source_run_metadata_sha256, label="source-run metadata")
    supplied: dict[str, Mapping[str, Any] | None] = {
        "activation_readiness": activation_readiness,
        "session_start": session_start,
        "supervisor": supervisor,
        "operational_entry": operational_entry,
        "recovery_receipt": recovery_receipt,
    }
    _validate_evidence_hashes(supplied, evidence_sha256)

    reasons = _validate_contiguous_chain(supplied)
    source_runs, run_reasons = _validate_run_metadata(
        supplied=supplied,
        source_run_metadata=source_run_metadata,
        git_sha=git_sha,
    )
    reasons.extend(run_reasons)
    if reasons:
        return _blocked(
            git_sha,
            evidence_sha256,
            source_runs,
            source_run_metadata_sha256,
            reasons,
        )

    _validate_activation_readiness(activation_readiness, git_sha, reasons)
    if reasons:
        return _blocked(
            git_sha,
            evidence_sha256,
            source_runs,
            source_run_metadata_sha256,
            reasons,
        )

    if session_start is None:
        return _partial(
            BybitDemoOperationalReleaseStage.INFRA_READY,
            git_sha,
            evidence_sha256,
            source_runs,
            source_run_metadata_sha256,
            next_required="session_start",
        )
    _validate_session_start(session_start, git_sha, reasons)
    if reasons:
        return _blocked(
            git_sha,
            evidence_sha256,
            source_runs,
            source_run_metadata_sha256,
            reasons,
        )

    if supervisor is None:
        return _partial(
            BybitDemoOperationalReleaseStage.SESSION_READY,
            git_sha,
            evidence_sha256,
            source_runs,
            source_run_metadata_sha256,
            next_required="supervisor",
        )
    _validate_supervisor(supervisor, git_sha, reasons)
    if reasons:
        return _blocked(
            git_sha,
            evidence_sha256,
            source_runs,
            source_run_metadata_sha256,
            reasons,
        )

    if operational_entry is None:
        return _partial(
            BybitDemoOperationalReleaseStage.SUPERVISOR_READY,
            git_sha,
            evidence_sha256,
            source_runs,
            source_run_metadata_sha256,
            next_required="operational_entry",
        )
    _validate_operational_entry(operational_entry, git_sha, reasons)
    if reasons:
        return _blocked(
            git_sha,
            evidence_sha256,
            source_runs,
            source_run_metadata_sha256,
            reasons,
        )

    if recovery_receipt is None:
        return _partial(
            BybitDemoOperationalReleaseStage.DEMO_ENTRY_PROVEN,
            git_sha,
            evidence_sha256,
            source_runs,
            source_run_metadata_sha256,
            next_required="recovery_receipt",
        )
    _validate_recovery_receipt(recovery_receipt, git_sha, reasons)
    _validate_post_entry_recovery_temporal_link(
        operational_entry,
        recovery_receipt,
        reasons,
    )
    if reasons:
        return _blocked(
            git_sha,
            evidence_sha256,
            source_runs,
            source_run_metadata_sha256,
            reasons,
        )

    return BybitDemoOperationalReleaseEvidence(
        stage=BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN,
        reasons=(),
        git_sha=git_sha,
        evidence_sha256=dict(evidence_sha256),
        source_runs=source_runs,
        source_run_metadata_sha256=source_run_metadata_sha256,
        next_required_evidence=None,
        release_gate_complete=True,
    )


def load_json_evidence(path: str | Path) -> tuple[dict[str, Any], str]:
    target = Path(path)
    raw = target.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Bybit Demo operational release evidence must be a JSON object")
    return payload, hashlib.sha256(raw).hexdigest()


def _partial(
    stage: BybitDemoOperationalReleaseStage,
    git_sha: str,
    evidence_sha256: Mapping[str, str],
    source_runs: Mapping[str, Mapping[str, Any]],
    source_run_metadata_sha256: str,
    *,
    next_required: str,
) -> BybitDemoOperationalReleaseEvidence:
    return BybitDemoOperationalReleaseEvidence(
        stage=stage,
        reasons=(f"NEXT_REQUIRED_EVIDENCE:{next_required}",),
        git_sha=git_sha,
        evidence_sha256=dict(evidence_sha256),
        source_runs=source_runs,
        source_run_metadata_sha256=source_run_metadata_sha256,
        next_required_evidence=next_required,
        release_gate_complete=False,
    )


def _blocked(
    git_sha: str,
    evidence_sha256: Mapping[str, str],
    source_runs: Mapping[str, Mapping[str, Any]],
    source_run_metadata_sha256: str,
    reasons: list[str],
) -> BybitDemoOperationalReleaseEvidence:
    return BybitDemoOperationalReleaseEvidence(
        stage=BybitDemoOperationalReleaseStage.BLOCKED,
        reasons=tuple(reasons),
        git_sha=git_sha,
        evidence_sha256=dict(evidence_sha256),
        source_runs=source_runs,
        source_run_metadata_sha256=source_run_metadata_sha256,
        next_required_evidence=None,
        release_gate_complete=False,
    )


def _validate_evidence_hashes(
    supplied: Mapping[str, Mapping[str, Any] | None],
    evidence_sha256: Mapping[str, str],
) -> None:
    for name, payload in supplied.items():
        digest = evidence_sha256.get(name)
        if payload is None:
            if digest is not None:
                raise ValueError(f"Bybit Demo {name} hash was supplied without evidence")
            continue
        if digest is None:
            raise ValueError(f"Bybit Demo {name} evidence hash is required")
        _validate_sha256(digest, label=f"{name} evidence")
    unknown = set(evidence_sha256) - set(supplied)
    if unknown:
        raise ValueError("Bybit Demo operational release evidence contains unknown hash keys")


def _validate_contiguous_chain(
    supplied: Mapping[str, Mapping[str, Any] | None],
) -> list[str]:
    reasons: list[str] = []
    missing_seen = False
    for name in _SOURCE_ORDER:
        present = supplied[name] is not None
        if not present:
            missing_seen = True
        elif missing_seen:
            reasons.append(f"NON_CONTIGUOUS_EVIDENCE_CHAIN:{name}")
    return reasons


def _validate_run_metadata(
    *,
    supplied: Mapping[str, Mapping[str, Any] | None],
    source_run_metadata: Mapping[str, Any],
    git_sha: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    reasons: list[str] = []
    present_names = {name for name, payload in supplied.items() if payload is not None}
    metadata_names = set(source_run_metadata)
    for missing in sorted(present_names - metadata_names):
        reasons.append(f"SOURCE_RUN_METADATA_MISSING:{missing}")
    for extra in sorted(metadata_names - present_names):
        reasons.append(f"SOURCE_RUN_METADATA_WITHOUT_EVIDENCE:{extra}")

    normalized: dict[str, dict[str, Any]] = {}
    seen_run_ids: set[int] = set()
    prior_time: datetime | None = None
    for name in _SOURCE_ORDER:
        if name not in present_names:
            continue
        value = source_run_metadata.get(name)
        if not isinstance(value, Mapping):
            reasons.append(f"SOURCE_RUN_METADATA_INVALID:{name}")
            continue

        run_id = value.get("run_id")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            reasons.append(f"SOURCE_RUN_ID_INVALID:{name}")
            continue
        if run_id in seen_run_ids:
            reasons.append(f"SOURCE_RUN_ID_REUSED:{name}")
        seen_run_ids.add(run_id)

        expected_workflow = _WORKFLOW_NAMES[name]
        workflow_name = value.get("workflow_name")
        if workflow_name != expected_workflow:
            reasons.append(f"SOURCE_RUN_WORKFLOW_MISMATCH:{name}")
        if value.get("event") != "workflow_dispatch":
            reasons.append(f"SOURCE_RUN_NOT_MANUAL_DISPATCH:{name}")
        if value.get("conclusion") != "success":
            reasons.append(f"SOURCE_RUN_NOT_SUCCESSFUL:{name}")
        if value.get("head_sha") != git_sha:
            reasons.append(f"SOURCE_RUN_GIT_SHA_MISMATCH:{name}")

        try:
            started_at = _utc_datetime(value.get("run_started_at"))
        except ValueError:
            reasons.append(f"SOURCE_RUN_STARTED_AT_INVALID:{name}")
            continue
        if prior_time is not None and started_at <= prior_time:
            reasons.append(f"SOURCE_RUN_ORDER_INVALID:{name}")
        prior_time = started_at

        normalized[name] = {
            "run_id": run_id,
            "workflow_name": workflow_name,
            "event": value.get("event"),
            "conclusion": value.get("conclusion"),
            "head_sha": value.get("head_sha"),
            "run_started_at": started_at.astimezone(UTC).isoformat(),
        }
    return normalized, reasons


def _validate_activation_readiness(
    payload: Mapping[str, Any],
    git_sha: str,
    reasons: list[str],
) -> None:
    _validate_source_identity(
        payload,
        schema="BYBIT_DEMO_ACTIVATION_READINESS_V1",
        git_sha=git_sha,
        label="ACTIVATION_READINESS",
        reasons=reasons,
    )
    if payload.get("status") != "READY_FOR_EXPLICIT_ACTIVATION_GATES":
        reasons.append("ACTIVATION_READINESS_NOT_READY")
    if payload.get("passed") is not True or payload.get("reasons") != []:
        reasons.append("ACTIVATION_READINESS_NOT_CLEAN")
    if payload.get("ready_for_explicit_arm") is not True:
        reasons.append("ACTIVATION_READINESS_ARM_GATE_NOT_READY")
    if payload.get("ready_for_exact_trade_approval") is not True:
        reasons.append("ACTIVATION_READINESS_APPROVAL_GATE_NOT_READY")
    if payload.get("operator_action_required") is not True:
        reasons.append("ACTIVATION_READINESS_OPERATOR_GATE_MISSING")
    if payload.get("arm_performed") is not False:
        reasons.append("ACTIVATION_READINESS_ALREADY_ARMED")
    if payload.get("trade_actionable") is not False:
        reasons.append("ACTIVATION_READINESS_UNSAFE_ACTIONABILITY")
    if payload.get("order_write_performed") is not False:
        reasons.append("ACTIVATION_READINESS_PERFORMED_ORDER_WRITE")
    if payload.get("order_writes_supported") is not False:
        reasons.append("ACTIVATION_READINESS_UNSAFE_ORDER_CAPABILITY")
    _validate_embedded_manifest(payload, "ACTIVATION_READINESS", reasons)


def _validate_session_start(
    payload: Mapping[str, Any],
    git_sha: str,
    reasons: list[str],
) -> None:
    _validate_source_identity(
        payload,
        schema="BYBIT_DEMO_SESSION_START_V1",
        git_sha=git_sha,
        label="SESSION_START",
        reasons=reasons,
    )
    if payload.get("status") not in {"INITIALIZED", "INITIALIZED_NOW"}:
        reasons.append("SESSION_START_NOT_INITIALIZED")
    if payload.get("passed") is not True:
        reasons.append("SESSION_START_NOT_PASSED")
    if payload.get("session_initialized") is not True:
        reasons.append("SESSION_START_SESSION_NOT_INITIALIZED")
    if payload.get("worker_session_ready") is not True:
        reasons.append("SESSION_START_WORKER_NOT_READY")
    if payload.get("opening_equity_positive") is not True:
        reasons.append("SESSION_START_OPENING_EQUITY_NOT_POSITIVE")
    if payload.get("automatic_reset_allowed") is not False:
        reasons.append("SESSION_START_AUTOMATIC_RESET_ALLOWED")
    if payload.get("trading_credential_required") is not False:
        reasons.append("SESSION_START_UNEXPECTED_TRADING_CREDENTIAL")
    if payload.get("order_write_performed") is not False:
        reasons.append("SESSION_START_PERFORMED_ORDER_WRITE")
    if payload.get("order_writes_supported") is not False:
        reasons.append("SESSION_START_UNSAFE_ORDER_CAPABILITY")


def _validate_supervisor(
    payload: Mapping[str, Any],
    git_sha: str,
    reasons: list[str],
) -> None:
    _validate_source_identity(
        payload,
        schema="BYBIT_DEMO_PERSISTENT_SUPERVISOR_V1",
        git_sha=git_sha,
        label="SUPERVISOR",
        reasons=reasons,
    )
    if payload.get("status") != "IDLE_NO_ACTIVE_TRADE":
        reasons.append("SUPERVISOR_PRE_ENTRY_IDLE_PROOF_MISSING")
    if payload.get("blocked") is not False:
        reasons.append("SUPERVISOR_BLOCKED")
    if payload.get("new_entry_attempted") is not False:
        reasons.append("SUPERVISOR_ATTEMPTED_ENTRY")
    if payload.get("autonomous_entry_allowed") is not False:
        reasons.append("SUPERVISOR_AUTONOMOUS_ENTRY_ALLOWED")
    if payload.get("operator_approval_bypass_allowed") is not False:
        reasons.append("SUPERVISOR_APPROVAL_BYPASS_ALLOWED")
    if payload.get("same_invocation_additional_entry_allowed") is not False:
        reasons.append("SUPERVISOR_REPLACEMENT_ENTRY_ALLOWED")
    if payload.get("demo_only") is not True:
        reasons.append("SUPERVISOR_NOT_DEMO_ONLY")


def _validate_operational_entry(
    payload: Mapping[str, Any],
    git_sha: str,
    reasons: list[str],
) -> None:
    _validate_source_identity(
        payload,
        schema="BYBIT_DEMO_OPERATIONAL_ENTRY_EVIDENCE_V1",
        git_sha=git_sha,
        label="OPERATIONAL_ENTRY",
        reasons=reasons,
    )
    if payload.get("status") != "ENTRY_CYCLE_COMPLETE":
        reasons.append("OPERATIONAL_ENTRY_NOT_COMPLETE")
    if payload.get("authorization_persisted") is not True:
        reasons.append("OPERATIONAL_ENTRY_AUTHORIZATION_NOT_PERSISTED")
    else:
        _validate_optional_payload_sha(
            payload.get("authorization_record_sha256"),
            "OPERATIONAL_ENTRY_AUTHORIZATION_SHA_INVALID",
            reasons,
        )
    if payload.get("entry_provenance_persisted") is not True:
        reasons.append("OPERATIONAL_ENTRY_PROVENANCE_NOT_PERSISTED")
    else:
        _validate_optional_payload_sha(
            payload.get("entry_provenance_record_sha256"),
            "OPERATIONAL_ENTRY_PROVENANCE_SHA_INVALID",
            reasons,
        )
    reconciliation = payload.get("protection_reconciliation_status")
    if reconciliation not in {
        "CANONICAL_RUNTIME_RECONCILED",
        "RECOVERED_PROTECTED",
        "RECOVERED_FLATTENED",
    }:
        reasons.append("OPERATIONAL_ENTRY_EXECUTION_NOT_SAFELY_RECONCILED")
    if payload.get("protection_reconciliation_completed") is not True:
        reasons.append("OPERATIONAL_ENTRY_RECONCILIATION_INCOMPLETE")
    if payload.get("same_invocation_additional_entry_allowed") is not False:
        reasons.append("OPERATIONAL_ENTRY_REPLACEMENT_ALLOWED")
    if payload.get("fixed_egress_verified") is not True:
        reasons.append("OPERATIONAL_ENTRY_FIXED_EGRESS_NOT_VERIFIED")
    if payload.get("protected_dispatch_required") is not True:
        reasons.append("OPERATIONAL_ENTRY_PROTECTED_DISPATCH_MISSING")
    if payload.get("automatic_arm_allowed") is not False:
        reasons.append("OPERATIONAL_ENTRY_AUTOMATIC_ARM_ALLOWED")
    if payload.get("ranked_fallback_allowed") is not False:
        reasons.append("OPERATIONAL_ENTRY_RANKED_FALLBACK_ALLOWED")


def _validate_recovery_receipt(
    payload: Mapping[str, Any],
    git_sha: str,
    reasons: list[str],
) -> None:
    _validate_source_identity(
        payload,
        schema="BYBIT_DEMO_RUNTIME_LEASE_RECOVERY_RECEIPT_V1",
        git_sha=git_sha,
        label="RECOVERY_RECEIPT",
        reasons=reasons,
    )
    if payload.get("status") != "RECOVERED":
        reasons.append("RECOVERY_DRILL_NOT_NEWLY_PROVEN")
    _validate_optional_payload_sha(
        payload.get("recovery_id"),
        "RECOVERY_RECEIPT_ID_INVALID",
        reasons,
    )
    _validate_optional_payload_sha(
        payload.get("lease_owner_sha256"),
        "RECOVERY_RECEIPT_OWNER_SHA_INVALID",
        reasons,
    )
    if payload.get("immutable_audit") is not True:
        reasons.append("RECOVERY_RECEIPT_AUDIT_NOT_IMMUTABLE")
    if payload.get("automatic_recovery_allowed") is not False:
        reasons.append("RECOVERY_RECEIPT_AUTOMATIC_RECOVERY_ALLOWED")
    if payload.get("automatic_stale_takeover_allowed") is not False:
        reasons.append("RECOVERY_RECEIPT_STALE_TAKEOVER_ALLOWED")
    if payload.get("order_writes_supported") is not False:
        reasons.append("RECOVERY_RECEIPT_UNSAFE_ORDER_CAPABILITY")
    if payload.get("idempotent_existing_recovery") is not False:
        reasons.append("RECOVERY_RECEIPT_IDEMPOTENCY_INVALID")


def _validate_post_entry_recovery_temporal_link(
    operational_entry: Mapping[str, Any],
    recovery_receipt: Mapping[str, Any],
    reasons: list[str],
) -> None:
    try:
        entry_observed_at = _utc_datetime(operational_entry.get("observed_at"))
    except ValueError:
        reasons.append("OPERATIONAL_ENTRY_OBSERVED_AT_INVALID")
        return
    try:
        recovery_created_at = _utc_datetime(recovery_receipt.get("created_at"))
    except ValueError:
        reasons.append("RECOVERY_RECEIPT_CREATED_AT_INVALID")
        return
    if recovery_created_at <= entry_observed_at:
        reasons.append("RECOVERY_RECEIPT_NOT_AFTER_OPERATIONAL_ENTRY")


def _validate_source_identity(
    payload: Mapping[str, Any],
    *,
    schema: str,
    git_sha: str,
    label: str,
    reasons: list[str],
) -> None:
    if payload.get("schema") != schema:
        reasons.append(f"{label}_SCHEMA_INVALID")
    if payload.get("git_sha") != git_sha:
        reasons.append(f"{label}_GIT_SHA_MISMATCH")
    if payload.get("live_mainnet_order_routing_allowed") is not False:
        reasons.append(f"{label}_UNSAFE_MAINNET_CAPABILITY")


def _validate_embedded_manifest(
    payload: Mapping[str, Any],
    label: str,
    reasons: list[str],
) -> None:
    actual = payload.get("manifest_sha256")
    if not isinstance(actual, str):
        reasons.append(f"{label}_MANIFEST_SHA_MISSING")
        return
    without_manifest = dict(payload)
    without_manifest.pop("manifest_sha256", None)
    expected = _sha256_canonical_json(without_manifest)
    if actual != expected:
        reasons.append(f"{label}_MANIFEST_SHA_MISMATCH")


def _validate_optional_payload_sha(value: Any, reason: str, reasons: list[str]) -> None:
    if not isinstance(value, str) or not _is_sha256(value):
        reasons.append(reason)


def _utc_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run timestamp is required")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("run timestamp is invalid") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("run timestamp must be timezone-aware")
    return moment.astimezone(UTC)


def _validate_git_sha(value: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("Bybit Demo operational release Git SHA must be lowercase 40-char hex")


def _validate_sha256(value: str, *, label: str) -> None:
    if not _is_sha256(value):
        raise ValueError(f"Bybit Demo {label} SHA-256 is invalid")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _sha256_canonical_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BybitDemoOperationalReleaseEvidence",
    "BybitDemoOperationalReleaseStage",
    "assemble_bybit_demo_operational_release_evidence",
    "load_json_evidence",
]
