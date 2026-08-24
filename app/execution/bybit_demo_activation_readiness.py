from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


class BybitDemoActivationReadinessStatus(StrEnum):
    READY_FOR_EXPLICIT_ACTIVATION_GATES = "READY_FOR_EXPLICIT_ACTIVATION_GATES"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class BybitDemoActivationReadinessResult:
    status: BybitDemoActivationReadinessStatus
    reasons: tuple[str, ...]
    git_sha: str
    postgres_evidence_sha256: str
    connected_preflight_evidence_sha256: str
    trading_credential_evidence_sha256: str
    control_status_evidence_sha256: str
    postgres_status: str
    connected_preflight_status: str
    trading_credential_status: str
    control_mode: str
    ready_for_explicit_arm: bool
    ready_for_exact_trade_approval: bool
    operator_action_required: bool = True
    arm_performed: bool = False
    trade_actionable: bool = False
    order_write_performed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    @property
    def passed(self) -> bool:
        ready = (
            BybitDemoActivationReadinessStatus.READY_FOR_EXPLICIT_ACTIVATION_GATES
        )
        return self.status is ready

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema": "BYBIT_DEMO_ACTIVATION_READINESS_V1",
            "status": self.status.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "git_sha": self.git_sha,
            "evidence": {
                "postgres_sha256": self.postgres_evidence_sha256,
                "connected_preflight_sha256": self.connected_preflight_evidence_sha256,
                "trading_credential_sha256": self.trading_credential_evidence_sha256,
                "control_status_sha256": self.control_status_evidence_sha256,
            },
            "postgres_status": self.postgres_status,
            "connected_preflight_status": self.connected_preflight_status,
            "trading_credential_status": self.trading_credential_status,
            "control_mode": self.control_mode,
            "ready_for_explicit_arm": self.ready_for_explicit_arm,
            "ready_for_exact_trade_approval": self.ready_for_exact_trade_approval,
            "operator_action_required": self.operator_action_required,
            "arm_performed": self.arm_performed,
            "trade_actionable": self.trade_actionable,
            "order_write_performed": self.order_write_performed,
            "order_writes_supported": self.order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }
        payload["manifest_sha256"] = _sha256_canonical_json(payload)
        return payload


def assemble_bybit_demo_activation_readiness(
    *,
    git_sha: str,
    postgres_payload: Mapping[str, Any],
    connected_preflight_payload: Mapping[str, Any],
    trading_credential_payload: Mapping[str, Any],
    control_status_payload: Mapping[str, Any],
    evidence_sha256: Mapping[str, str],
) -> BybitDemoActivationReadinessResult:
    """Fail-closed infrastructure readiness; performs no ARM, approval or order mutation."""

    _validate_git_sha(git_sha)
    for name in ("postgres", "connected", "credential", "control"):
        value = evidence_sha256.get(name, "")
        _validate_sha256(value, label=f"{name} evidence")

    reasons: list[str] = []
    _validate_postgres(postgres_payload, reasons)
    _validate_connected(connected_preflight_payload, reasons)
    _validate_credential(trading_credential_payload, reasons)
    control_mode = _validate_control(control_status_payload, reasons)

    postgres_status = _safe_string(postgres_payload.get("status"))
    connected_status = _safe_string(connected_preflight_payload.get("status"))
    credential_status = _safe_string(trading_credential_payload.get("status"))
    ready = not reasons
    return BybitDemoActivationReadinessResult(
        status=(
            BybitDemoActivationReadinessStatus.READY_FOR_EXPLICIT_ACTIVATION_GATES
            if ready
            else BybitDemoActivationReadinessStatus.BLOCKED
        ),
        reasons=tuple(reasons),
        git_sha=git_sha,
        postgres_evidence_sha256=evidence_sha256["postgres"],
        connected_preflight_evidence_sha256=evidence_sha256["connected"],
        trading_credential_evidence_sha256=evidence_sha256["credential"],
        control_status_evidence_sha256=evidence_sha256["control"],
        postgres_status=postgres_status,
        connected_preflight_status=connected_status,
        trading_credential_status=credential_status,
        control_mode=control_mode,
        ready_for_explicit_arm=ready,
        ready_for_exact_trade_approval=ready,
    )


def load_json_evidence(path: str | Path) -> tuple[dict[str, Any], str]:
    target = Path(path)
    raw = target.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Bybit Demo readiness evidence must be a JSON object")
    return payload, hashlib.sha256(raw).hexdigest()


def _validate_postgres(payload: Mapping[str, Any], reasons: list[str]) -> None:
    if payload.get("schema") != "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V2":
        reasons.append("POSTGRES_EVIDENCE_SCHEMA_INVALID")
        return
    if payload.get("mode") != "verify":
        reasons.append("POSTGRES_EVIDENCE_NOT_VERIFY_MODE")
    if payload.get("status") != "VERIFIED_READY" or payload.get("passed") is not True:
        reasons.append("POSTGRES_SCHEMA_NOT_VERIFIED_READY")
    if payload.get("schema_mutation_performed") is not False:
        reasons.append("POSTGRES_READINESS_PERFORMED_MUTATION")
    if payload.get("bybit_order_writes_supported") is not False:
        reasons.append("POSTGRES_EVIDENCE_UNSAFE_ORDER_CAPABILITY")
    if payload.get("live_mainnet_order_routing_allowed") is not False:
        reasons.append("POSTGRES_EVIDENCE_UNSAFE_MAINNET_CAPABILITY")


def _validate_connected(payload: Mapping[str, Any], reasons: list[str]) -> None:
    if payload.get("schema") != "BYBIT_DEMO_CONNECTED_PREFLIGHT_V1":
        reasons.append("CONNECTED_PREFLIGHT_EVIDENCE_SCHEMA_INVALID")
        return
    if payload.get("status") != "READY_FOR_MANUAL_OPERATOR_APPROVAL":
        reasons.append("CONNECTED_PREFLIGHT_NOT_READY")
    if payload.get("reasons") != []:
        reasons.append("CONNECTED_PREFLIGHT_HAS_REASONS")
    if payload.get("fixed_egress_required") is not True:
        reasons.append("CONNECTED_PREFLIGHT_NOT_FIXED_EGRESS")
    if payload.get("read_only_api_key_verified") is not True:
        reasons.append("CONNECTED_PREFLIGHT_KEY_NOT_READ_ONLY")
    if payload.get("api_key_ip_binding_present") is not True:
        reasons.append("CONNECTED_PREFLIGHT_KEY_NOT_IP_BOUND")
    if (
        payload.get("preflight_only") is not True
        or payload.get("trade_actionable") is not False
    ):
        reasons.append("CONNECTED_PREFLIGHT_UNSAFE_ACTIONABILITY")
    if payload.get("order_writes_supported") is not False:
        reasons.append("CONNECTED_PREFLIGHT_UNSAFE_ORDER_CAPABILITY")
    if payload.get("live_mainnet_order_routing_allowed") is not False:
        reasons.append("CONNECTED_PREFLIGHT_UNSAFE_MAINNET_CAPABILITY")


def _validate_credential(payload: Mapping[str, Any], reasons: list[str]) -> None:
    if payload.get("schema") != "BYBIT_DEMO_TRADING_CREDENTIAL_PREFLIGHT_V1":
        reasons.append("TRADING_CREDENTIAL_EVIDENCE_SCHEMA_INVALID")
        return
    if (
        payload.get("status")
        != "READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL"
        or payload.get("passed") is not True
    ):
        reasons.append("TRADING_CREDENTIAL_NOT_READY")
    required_true = (
        "write_enabled_verified",
        "ip_binding_present",
        "personal_key_type_verified",
        "uta_enabled",
        "contract_order_permission",
        "contract_position_permission",
        "least_privilege_contract_only",
        "distinct_from_demo_readonly_key",
        "distinct_from_mainnet_readonly_key",
        "authenticated_get_only",
    )
    if any(payload.get(name) is not True for name in required_true):
        reasons.append("TRADING_CREDENTIAL_SHAPE_INCOMPLETE")
    if payload.get("order_write_performed") is not False:
        reasons.append("TRADING_CREDENTIAL_PREFLIGHT_PERFORMED_ORDER_WRITE")
    if payload.get("order_writes_supported") is not False:
        reasons.append("TRADING_CREDENTIAL_PREFLIGHT_EXPOSES_ORDER_WRITES")
    if payload.get("live_mainnet_order_routing_allowed") is not False:
        reasons.append("TRADING_CREDENTIAL_PREFLIGHT_EXPOSES_MAINNET")


def _validate_control(payload: Mapping[str, Any], reasons: list[str]) -> str:
    if payload.get("schema") != "BYBIT_DEMO_CONTROL_OPERATION_V1":
        reasons.append("CONTROL_STATUS_EVIDENCE_SCHEMA_INVALID")
        return "UNKNOWN"
    if payload.get("mode") != "status" or payload.get("status") != "STATUS_READ":
        reasons.append("CONTROL_STATUS_READ_INVALID")
    if (
        payload.get("passed") is not True
        or payload.get("fixed_egress_required") is not True
    ):
        reasons.append("CONTROL_STATUS_NOT_FIXED_EGRESS_READY")
    if payload.get("order_writes_supported") is not False:
        reasons.append("CONTROL_STATUS_UNSAFE_ORDER_CAPABILITY")
    if payload.get("live_mainnet_order_routing_allowed") is not False:
        reasons.append("CONTROL_STATUS_UNSAFE_MAINNET_CAPABILITY")
    decision = payload.get("decision")
    if not isinstance(decision, Mapping):
        reasons.append("CONTROL_DECISION_MISSING")
        return "UNKNOWN"
    mode = _safe_string(decision.get("mode"))
    if mode != "HALTED" or decision.get("new_entry_allowed") is not False:
        reasons.append("CONTROL_PLANE_MUST_BE_HALTED_BEFORE_ACTIVATION")
    if decision.get("order_writes_supported") is not False:
        reasons.append("CONTROL_DECISION_UNSAFE_ORDER_CAPABILITY")
    if decision.get("live_mainnet_order_routing_allowed") is not False:
        reasons.append("CONTROL_DECISION_UNSAFE_MAINNET_CAPABILITY")
    return mode


def _safe_string(value: Any) -> str:
    return value if isinstance(value, str) else "UNKNOWN"


def _validate_git_sha(value: str) -> None:
    valid_hex = all(char in "0123456789abcdef" for char in value)
    if len(value) != 40 or not valid_hex:
        raise ValueError(
            "Bybit Demo activation readiness git SHA must be lowercase 40-char hex"
        )


def _validate_sha256(value: str, *, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"Bybit Demo {label} SHA-256 is invalid")


def _sha256_canonical_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BybitDemoActivationReadinessResult",
    "BybitDemoActivationReadinessStatus",
    "assemble_bybit_demo_activation_readiness",
    "load_json_evidence",
]