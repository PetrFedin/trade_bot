from __future__ import annotations

import app.execution.bybit_demo_operational_release_account_binding as binding
from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseEvidence,
    BybitDemoOperationalReleaseStage,
)


def _result(stage: BybitDemoOperationalReleaseStage) -> BybitDemoOperationalReleaseEvidence:
    return BybitDemoOperationalReleaseEvidence(
        stage=stage,
        reasons=(),
        git_sha="a" * 40,
        evidence_sha256={},
        source_runs={},
        source_run_metadata_sha256="f" * 64,
        next_required_evidence=None,
        release_gate_complete=stage is BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN,
    )


def _assemble(monkeypatch, result, *, activation=True, supervisor=None, entry=None):
    monkeypatch.setattr(
        binding,
        "assemble_checkpoint_bound_bybit_demo_operational_release_evidence",
        lambda **_kwargs: result,
    )
    return binding.assemble_account_bound_bybit_demo_operational_release_evidence(
        git_sha="a" * 40,
        activation_readiness={"demo_account_identity_verified": activation},
        evidence_sha256={},
        source_run_metadata={},
        source_run_metadata_sha256="f" * 64,
        supervisor=(
            None
            if supervisor is None
            else {"demo_account_identity_verified": supervisor}
        ),
        operational_entry=(
            None
            if entry is None
            else {"demo_account_identity_verified": entry}
        ),
    )


def test_verified_account_identity_preserves_release_result(monkeypatch) -> None:
    proven = _result(BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN)

    result = _assemble(
        monkeypatch,
        proven,
        activation=True,
        supervisor=True,
        entry=True,
    )

    assert result is proven
    assert result.release_gate_complete is True


def test_activation_without_same_account_proof_fails_closed(monkeypatch) -> None:
    result = _assemble(
        monkeypatch,
        _result(BybitDemoOperationalReleaseStage.INFRA_READY),
        activation=False,
    )

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert result.reasons == ("ACTIVATION_READINESS_ACCOUNT_IDENTITY_NOT_VERIFIED",)
    assert result.release_gate_complete is False


def test_supervisor_without_same_account_proof_fails_closed(monkeypatch) -> None:
    result = _assemble(
        monkeypatch,
        _result(BybitDemoOperationalReleaseStage.SUPERVISOR_READY),
        activation=True,
        supervisor=False,
    )

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert result.reasons == ("SUPERVISOR_ACCOUNT_IDENTITY_NOT_VERIFIED",)


def test_entry_without_same_account_proof_fails_closed(monkeypatch) -> None:
    result = _assemble(
        monkeypatch,
        _result(BybitDemoOperationalReleaseStage.DEMO_ENTRY_PROVEN),
        activation=True,
        supervisor=True,
        entry=False,
    )

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert result.reasons == ("OPERATIONAL_ENTRY_ACCOUNT_IDENTITY_NOT_VERIFIED",)


def test_missing_legacy_account_fields_are_not_treated_as_verified(monkeypatch) -> None:
    monkeypatch.setattr(
        binding,
        "assemble_checkpoint_bound_bybit_demo_operational_release_evidence",
        lambda **_kwargs: _result(BybitDemoOperationalReleaseStage.DEMO_ENTRY_PROVEN),
    )

    result = binding.assemble_account_bound_bybit_demo_operational_release_evidence(
        git_sha="a" * 40,
        activation_readiness={},
        evidence_sha256={},
        source_run_metadata={},
        source_run_metadata_sha256="f" * 64,
        supervisor={},
        operational_entry={},
    )

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert result.reasons == (
        "ACTIVATION_READINESS_ACCOUNT_IDENTITY_NOT_VERIFIED",
        "SUPERVISOR_ACCOUNT_IDENTITY_NOT_VERIFIED",
        "OPERATIONAL_ENTRY_ACCOUNT_IDENTITY_NOT_VERIFIED",
    )
