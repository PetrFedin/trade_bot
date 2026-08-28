from __future__ import annotations

from dataclasses import replace

import app.execution.bybit_demo_operational_release_logical_db_binding as gate
from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseEvidence,
    BybitDemoOperationalReleaseStage,
)
from app.execution.bybit_demo_operational_release_zone_binding import (
    BybitDemoOperationalZoneBoundReleaseEvidence,
)

_GIT_SHA = "a" * 40


def _base() -> BybitDemoOperationalReleaseEvidence:
    return BybitDemoOperationalReleaseEvidence(
        stage=BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN,
        reasons=(),
        git_sha=_GIT_SHA,
        evidence_sha256={"activation_readiness": "1" * 64},
        source_runs={},
        source_run_metadata_sha256="2" * 64,
        next_required_evidence=None,
        release_gate_complete=True,
    )


def _zone_success() -> BybitDemoOperationalZoneBoundReleaseEvidence:
    return BybitDemoOperationalZoneBoundReleaseEvidence(
        base=_base(),
        zone_binding_sha256={"activation_readiness": "3" * 64},
        operational_zone_binding_verified=True,
    )


def _assemble(monkeypatch, zone_payload):
    captured = {}

    def _legacy(**kwargs):
        captured.update(kwargs)
        return _zone_success()

    monkeypatch.setattr(
        gate,
        "assemble_zone_bound_bybit_demo_operational_release_evidence",
        _legacy,
    )
    payload = {"demo_account_identity_verified": True}
    result = gate.assemble_logical_db_bound_bybit_demo_operational_release_evidence(
        git_sha=_GIT_SHA,
        activation_readiness=payload,
        evidence_sha256={"activation_readiness": "1" * 64},
        source_run_metadata={},
        source_run_metadata_sha256="2" * 64,
        zone_bindings={"activation_readiness": zone_payload},
        zone_binding_sha256={"activation_readiness": "3" * 64},
    )
    return result, captured


def test_v2_zone_sidecar_with_logical_identity_delegates_to_v1_continuity(monkeypatch) -> None:
    result, captured = _assemble(
        monkeypatch,
        {
            "schema": "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V2",
            "logical_database_identity_verified": True,
        },
    )

    assert result.stage is BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN
    assert result.operational_zone_binding_verified is True
    normalized = captured["zone_bindings"]["activation_readiness"]
    assert normalized["schema"] == "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V1"
    assert "logical_database_identity_verified" not in normalized


def test_legacy_v1_zone_sidecar_is_rejected(monkeypatch) -> None:
    result, _captured = _assemble(
        monkeypatch,
        {
            "schema": "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V1",
            "logical_database_identity_verified": True,
        },
    )

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert result.operational_zone_binding_verified is False
    assert (
        "OPERATIONAL_ZONE_V124_SCHEMA_INVALID:activation_readiness"
        in result.base.reasons
    )


def test_sidecar_without_verified_logical_identity_is_rejected(monkeypatch) -> None:
    result, _captured = _assemble(
        monkeypatch,
        {
            "schema": "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V2",
            "logical_database_identity_verified": False,
        },
    )

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert (
        "OPERATIONAL_ZONE_LOGICAL_DB_IDENTITY_MISSING:activation_readiness"
        in result.base.reasons
    )


def test_v124_reason_is_preserved_when_legacy_zone_gate_already_blocked(monkeypatch) -> None:
    blocked = replace(
        _zone_success(),
        base=replace(
            _base(),
            stage=BybitDemoOperationalReleaseStage.BLOCKED,
            reasons=("OPERATIONAL_ZONE_DATABASE_DRIFT",),
            release_gate_complete=False,
        ),
        operational_zone_binding_verified=False,
    )
    monkeypatch.setattr(
        gate,
        "assemble_zone_bound_bybit_demo_operational_release_evidence",
        lambda **_kwargs: blocked,
    )
    payload = {"demo_account_identity_verified": True}
    result = gate.assemble_logical_db_bound_bybit_demo_operational_release_evidence(
        git_sha=_GIT_SHA,
        activation_readiness=payload,
        evidence_sha256={"activation_readiness": "1" * 64},
        source_run_metadata={},
        source_run_metadata_sha256="2" * 64,
        zone_bindings={
            "activation_readiness": {
                "schema": "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V1",
                "logical_database_identity_verified": False,
            }
        },
        zone_binding_sha256={"activation_readiness": "3" * 64},
    )

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "OPERATIONAL_ZONE_DATABASE_DRIFT" in result.base.reasons
    assert (
        "OPERATIONAL_ZONE_V124_SCHEMA_INVALID:activation_readiness"
        in result.base.reasons
    )
    assert (
        "OPERATIONAL_ZONE_LOGICAL_DB_IDENTITY_MISSING:activation_readiness"
        in result.base.reasons
    )
