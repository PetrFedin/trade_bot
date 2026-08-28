from __future__ import annotations

import app.execution.bybit_demo_activation_readiness_v124 as gate
from app.execution.bybit_demo_activation_readiness import (
    BybitDemoActivationReadinessResult,
    BybitDemoActivationReadinessStatus,
)

_GIT_SHA = "a" * 40


def _ready() -> BybitDemoActivationReadinessResult:
    return BybitDemoActivationReadinessResult(
        status=BybitDemoActivationReadinessStatus.READY_FOR_EXPLICIT_ACTIVATION_GATES,
        reasons=(),
        git_sha=_GIT_SHA,
        postgres_evidence_sha256="1" * 64,
        connected_preflight_evidence_sha256="2" * 64,
        trading_credential_evidence_sha256="3" * 64,
        same_account_evidence_sha256="4" * 64,
        control_status_evidence_sha256="5" * 64,
        postgres_status="VERIFIED_READY",
        connected_preflight_status="READY_FOR_MANUAL_OPERATOR_APPROVAL",
        trading_credential_status="READY_FOR_OPERATOR_GATED_DEMO_WORKER_CREDENTIAL",
        same_account_status="SAME_ACCOUNT_VERIFIED",
        control_mode="HALTED",
        demo_account_identity_verified=True,
        ready_for_explicit_arm=True,
        ready_for_exact_trade_approval=True,
    )


def _assemble(monkeypatch, postgres):
    captured = {}

    def _base(**kwargs):
        captured.update(kwargs)
        return _ready()

    monkeypatch.setattr(gate, "assemble_bybit_demo_activation_readiness", _base)
    result = gate.assemble_v124_bybit_demo_activation_readiness(
        git_sha=_GIT_SHA,
        postgres_payload=postgres,
        connected_preflight_payload={},
        trading_credential_payload={},
        same_account_payload={},
        control_status_payload={},
        evidence_sha256={
            "postgres": "1" * 64,
            "connected": "2" * 64,
            "credential": "3" * 64,
            "same_account": "4" * 64,
            "control": "5" * 64,
        },
    )
    return result, captured


def test_v124_verified_identity_allows_existing_readiness_contract(monkeypatch) -> None:
    result, captured = _assemble(
        monkeypatch,
        {
            "schema": "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V4",
            "logical_database_identity_verified": True,
        },
    )

    assert result.passed is True
    assert result.ready_for_explicit_arm is True
    normalized = captured["postgres_payload"]
    assert normalized["schema"] == "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V3"
    assert "logical_database_identity_verified" not in normalized


def test_legacy_bootstrap_schema_fails_closed(monkeypatch) -> None:
    result, _captured = _assemble(
        monkeypatch,
        {
            "schema": "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V3",
            "logical_database_identity_verified": True,
        },
    )

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert "POSTGRES_V124_EVIDENCE_SCHEMA_INVALID" in result.reasons
    assert result.ready_for_explicit_arm is False
    assert result.ready_for_exact_trade_approval is False


def test_missing_logical_database_identity_fails_closed(monkeypatch) -> None:
    result, _captured = _assemble(
        monkeypatch,
        {
            "schema": "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V4",
            "logical_database_identity_verified": False,
        },
    )

    assert result.status is BybitDemoActivationReadinessStatus.BLOCKED
    assert "POSTGRES_LOGICAL_DATABASE_IDENTITY_NOT_VERIFIED" in result.reasons
    assert result.ready_for_explicit_arm is False
