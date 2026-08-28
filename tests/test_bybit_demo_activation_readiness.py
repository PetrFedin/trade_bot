from __future__ import annotations

import json
from pathlib import Path

from app.execution.bybit_demo_activation_readiness import (
    BybitDemoActivationReadinessStatus,
    assemble_bybit_demo_activation_readiness,
    load_json_evidence,
)

_GIT_SHA = "a" * 40
_EVIDENCE_SHA = {
    "postgres": "1" * 64,
    "connected": "2" * 64,
    "credential": "3" * 64,
    "same_account": "4" * 64,
    "control": "5" * 64,
}


def _postgres() -> dict[str, object]:
    return {
        "schema": "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V3",
        "status": "VERIFIED_READY",
        "passed": True,
        "mode": "verify",
        "schema_mutation_performed": False,
        "required_relations_present": True,
        "append_only_triggers_present": True,
        "database_identity_exposed": False,
        "bybit_credentials_required": False,
        "bybit_order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _connected() -> dict[str, object]:
    return {
        "schema": "BYBIT_DEMO_CONNECTED_PREFLIGHT_V1",
        "status": "READY_FOR_MANUAL_OPERATOR_APPROVAL",
        "reasons": [],
        "fixed_egress_required": True,
        "read_only_api_key_verified": True,
        "api_key_ip_binding_present": True,
        "preflight_only": True,
        "trade_actionable": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _credential() -> dict[str, object]:
    return {
        "schema": "BYBIT_DEMO_TRADING_CREDENTIAL_PREFLIGHT_V1",
        "status": "READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL",
        "passed": True,
        "reasons": [],
        "write_enabled_verified": True,
        "ip_binding_present": True,
        "personal_key_type_verified": True,
        "uta_enabled": True,
        "contract_order_permission": True,
        "contract_position_permission": True,
        "least_privilege_contract_only": True,
        "distinct_from_demo_readonly_key": True,
        "distinct_from_mainnet_readonly_key": True,
        "authenticated_get_only": True,
        "order_write_performed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _same_account() -> dict[str, object]:
    return {
        "schema": "BYBIT_DEMO_SAME_ACCOUNT_PREFLIGHT_V1",
        "status": "VERIFIED_SAME_ACCOUNT",
        "passed": True,
        "reasons": [],
        "same_user_id": True,
        "same_parent_uid": True,
        "same_master_scope": True,
        "authenticated_get_only": True,
        "order_write_performed": False,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
        "git_sha": _GIT_SHA,
    }


def _control() -> dict[str, object]:
    return {
        "schema": "BYBIT_DEMO_CONTROL_OPERATION_V1",
        "mode": "status",
        "status": "STATUS_READ",
        "passed": True,
        "fixed_egress_required": True,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
        "decision": {
            "schema": "BYBIT_DEMO_CONTROL_DECISION_V1",
            "mode": "HALTED",
            "reasons": ["DEMO_CONTROL_NO_EVENT_DEFAULT_HALT"],
            "new_entry_allowed": False,
            "order_writes_supported": False,
            "live_mainnet_order_routing_allowed": False,
        },
    }


def _assemble(
    *,
    postgres: dict[str, object] | None = None,
    connected: dict[str, object] | None = None,
    credential: dict[str, object] | None = None,
    same_account: dict[str, object] | None = None,
    control: dict[str, object] | None = None,
):
    return assemble_bybit_demo_activation_readiness(
        git_sha=_GIT_SHA,
        postgres_payload=_postgres() if postgres is None else postgres,
        connected_preflight_payload=_connected() if connected is None else connected,
        trading_credential_payload=_credential() if credential is None else credential,
        same_account_payload=_same_account() if same_account is None else same_account,
        control_status_payload=_control() if control is None else control,
        evidence_sha256=_EVIDENCE_SHA,
    )


def test_all_safe_evidence_is_ready_but_never_actionable() -> None:
    result = _assemble()

    ready = BybitDemoActivationReadinessStatus.READY_FOR_EXPLICIT_ACTIVATION_GATES
    assert result.status is ready
    assert result.passed is True
    assert result.reasons == ()
    assert result.demo_account_identity_verified is True
    assert result.same_account_status == "VERIFIED_SAME_ACCOUNT"
    assert result.ready_for_explicit_arm is True
    assert result.ready_for_exact_trade_approval is True
    assert result.operator_action_required is True
    assert result.arm_performed is False
    assert result.trade_actionable is False
    assert result.order_write_performed is False
    assert result.order_writes_supported is False
    assert result.live_mainnet_order_routing_allowed is False

    first = result.to_payload()
    second = result.to_payload()
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert len(first["manifest_sha256"]) == 64
    assert first["evidence"]["postgres_sha256"] == _EVIDENCE_SHA["postgres"]
    assert first["evidence"]["same_account_sha256"] == _EVIDENCE_SHA["same_account"]


def test_v122_bootstrap_evidence_is_rejected_after_v123_upgrade() -> None:
    postgres = _postgres()
    postgres["schema"] = "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V2"

    result = _assemble(postgres=postgres)

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert result.reasons == ("POSTGRES_EVIDENCE_SCHEMA_INVALID",)
    assert result.ready_for_explicit_arm is False
    assert result.ready_for_exact_trade_approval is False


def test_postgres_must_be_read_only_verified_ready() -> None:
    postgres = _postgres()
    postgres["status"] = "APPLIED_AND_VERIFIED"
    postgres["schema_mutation_performed"] = True

    result = _assemble(postgres=postgres)

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert "POSTGRES_SCHEMA_NOT_VERIFIED_READY" in result.reasons
    assert "POSTGRES_READINESS_PERFORMED_MUTATION" in result.reasons


def test_connected_preflight_requires_fixed_egress_ip_bound_clean_state() -> None:
    connected = _connected()
    connected["fixed_egress_required"] = False
    connected["api_key_ip_binding_present"] = False
    connected["reasons"] = ["DEMO_READONLY_API_KEY_HAS_NO_IP_BINDING"]

    result = _assemble(connected=connected)

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert "CONNECTED_PREFLIGHT_HAS_REASONS" in result.reasons
    assert "CONNECTED_PREFLIGHT_NOT_FIXED_EGRESS" in result.reasons
    assert "CONNECTED_PREFLIGHT_KEY_NOT_IP_BOUND" in result.reasons


def test_existing_trade_management_state_is_not_new_entry_readiness() -> None:
    connected = _connected()
    connected["status"] = "EXISTING_TRADE_MANAGEMENT_REQUIRED"

    result = _assemble(connected=connected)

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert "CONNECTED_PREFLIGHT_NOT_READY" in result.reasons
    assert result.ready_for_explicit_arm is False


def test_trading_credential_must_be_least_privilege_and_namespace_distinct() -> None:
    credential = _credential()
    credential["least_privilege_contract_only"] = False
    credential["distinct_from_mainnet_readonly_key"] = False

    result = _assemble(credential=credential)

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert "TRADING_CREDENTIAL_SHAPE_INCOMPLETE" in result.reasons


def test_different_demo_accounts_block_activation_readiness() -> None:
    same_account = _same_account()
    same_account["status"] = "BLOCKED"
    same_account["passed"] = False
    same_account["reasons"] = ["DEMO_CREDENTIAL_USER_ID_MISMATCH"]
    same_account["same_user_id"] = False

    result = _assemble(same_account=same_account)

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert "DEMO_CREDENTIALS_NOT_SAME_ACCOUNT" in result.reasons
    assert "SAME_ACCOUNT_EVIDENCE_HAS_REASONS" in result.reasons
    assert "SAME_ACCOUNT_EVIDENCE_INCOMPLETE" in result.reasons
    assert result.demo_account_identity_verified is False
    assert result.ready_for_explicit_arm is False


def test_same_account_proof_must_be_bound_to_exact_git_sha() -> None:
    same_account = _same_account()
    same_account["git_sha"] = "b" * 40

    result = _assemble(same_account=same_account)

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert "SAME_ACCOUNT_EVIDENCE_GIT_SHA_MISMATCH" in result.reasons


def test_control_plane_must_be_halted_before_activation_gates() -> None:
    control = _control()
    decision = control["decision"]
    assert isinstance(decision, dict)
    decision["mode"] = "ARMED_NEW_ENTRIES"
    decision["new_entry_allowed"] = True

    result = _assemble(control=control)

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert "CONTROL_PLANE_MUST_BE_HALTED_BEFORE_ACTIVATION" in result.reasons
    assert result.control_mode == "ARMED_NEW_ENTRIES"


def test_unsafe_mainnet_flag_blocks_even_if_status_strings_are_ready() -> None:
    credential = _credential()
    credential["live_mainnet_order_routing_allowed"] = True

    result = _assemble(credential=credential)

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert "TRADING_CREDENTIAL_PREFLIGHT_EXPOSES_MAINNET" in result.reasons


def test_evidence_file_hashes_exact_bytes(tmp_path: Path) -> None:
    payload = _connected()
    target = tmp_path / "connected.json"
    target.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    loaded, digest = load_json_evidence(target)

    assert loaded == payload
    assert len(digest) == 64


def test_manifest_contains_no_source_evidence_contents() -> None:
    result = _assemble()
    payload = result.to_payload()
    serialized = json.dumps(payload, sort_keys=True)

    assert "203.0.113" not in serialized
    assert "apiKey" not in serialized
    assert "secret" not in serialized.lower()
    assert "123456" not in serialized
    assert "BYBIT_DEMO_DATABASE_DSN" not in serialized
