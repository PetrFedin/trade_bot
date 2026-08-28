from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

import app.execution.bybit_demo_operational_release_zone_binding as zone
from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseEvidence,
    BybitDemoOperationalReleaseStage,
)

_GIT_SHA = "a" * 40
_SOURCE_ORDER = (
    "activation_readiness",
    "session_start",
    "supervisor",
    "arm_control",
    "operational_entry",
    "halt_control",
    "recovery_receipt",
)
_ACCOUNT_BOUND = {
    "activation_readiness",
    "supervisor",
    "arm_control",
    "operational_entry",
    "halt_control",
}
_DB_TOKEN = "1" * 64
_ACCOUNT_TOKEN = "2" * 64
_KEY_MARKER = "3" * 64


def _base(stage: BybitDemoOperationalReleaseStage) -> BybitDemoOperationalReleaseEvidence:
    return BybitDemoOperationalReleaseEvidence(
        stage=stage,
        reasons=(),
        git_sha=_GIT_SHA,
        evidence_sha256={name: "4" * 64 for name in _SOURCE_ORDER},
        source_runs=_run_metadata(),
        source_run_metadata_sha256="5" * 64,
        next_required_evidence=None,
        release_gate_complete=stage is BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN,
    )


def _run_metadata() -> dict[str, dict[str, object]]:
    start = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    return {
        name: {
            "run_started_at": (start + timedelta(minutes=index)).isoformat(),
            "run_completed_at": (start + timedelta(minutes=index, seconds=40)).isoformat(),
        }
        for index, name in enumerate(_SOURCE_ORDER)
    }


def _binding(name: str, *, index: int) -> dict[str, object]:
    observed = datetime(2026, 8, 28, 12, index, 20, tzinfo=UTC)
    account_required = name in _ACCOUNT_BOUND
    return {
        "schema": "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V1",
        "status": "BOUND",
        "passed": True,
        "producer": name,
        "git_sha": _GIT_SHA,
        "observed_at": observed.isoformat(),
        "binding_algorithm": "HMAC-SHA256",
        "binding_key_marker_sha256": _KEY_MARKER,
        "database_binding_present": True,
        "database_binding_sha256": _DB_TOKEN,
        "demo_account_binding_present": account_required,
        "demo_account_binding_sha256": _ACCOUNT_TOKEN if account_required else None,
        "order_writes_supported": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _bindings(count: int = 7) -> dict[str, dict[str, object]]:
    return {
        name: _binding(name, index=index)
        for index, name in enumerate(_SOURCE_ORDER[:count])
    }


def _hashes(count: int = 7) -> dict[str, str]:
    return {name: f"{index + 6:x}" * 64 for index, name in enumerate(_SOURCE_ORDER[:count])}


def _assemble(monkeypatch: pytest.MonkeyPatch, *, bindings=None, hashes=None):
    monkeypatch.setattr(
        zone,
        "assemble_account_bound_bybit_demo_operational_release_evidence",
        lambda **_kwargs: _base(BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN),
    )
    payload = {"git_sha": _GIT_SHA, "demo_account_identity_verified": True}
    return zone.assemble_zone_bound_bybit_demo_operational_release_evidence(
        git_sha=_GIT_SHA,
        activation_readiness=payload,
        evidence_sha256={name: "4" * 64 for name in _SOURCE_ORDER},
        source_run_metadata=_run_metadata(),
        source_run_metadata_sha256="5" * 64,
        zone_bindings=_bindings() if bindings is None else bindings,
        zone_binding_sha256=_hashes() if hashes is None else hashes,
        session_start=payload,
        supervisor=payload,
        arm_control=payload,
        operational_entry=payload,
        halt_control=payload,
        recovery_receipt=payload,
    )


def test_full_chain_accepts_one_exact_database_account_and_binding_key(monkeypatch) -> None:
    result = _assemble(monkeypatch)

    assert result.stage is BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN
    assert result.operational_zone_binding_verified is True
    output = result.to_payload()
    assert output["operational_zone_binding_verified"] is True
    assert output["zone_binding_sha256"] == _hashes()
    manifest = output.pop("manifest_sha256")
    encoded = json.dumps(
        output,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    assert manifest == hashlib.sha256(encoded).hexdigest()
    serialized = json.dumps(output, sort_keys=True)
    assert _DB_TOKEN not in serialized
    assert _ACCOUNT_TOKEN not in serialized
    assert _KEY_MARKER not in serialized


def test_database_drift_between_manual_runs_fails_closed(monkeypatch) -> None:
    bindings = _bindings()
    bindings["operational_entry"]["database_binding_sha256"] = "6" * 64

    result = _assemble(monkeypatch, bindings=bindings)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert result.operational_zone_binding_verified is False
    assert "OPERATIONAL_ZONE_DATABASE_DRIFT" in result.base.reasons


def test_demo_account_drift_between_credential_bearing_runs_fails_closed(monkeypatch) -> None:
    bindings = _bindings()
    bindings["arm_control"]["demo_account_binding_sha256"] = "7" * 64

    result = _assemble(monkeypatch, bindings=bindings)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "OPERATIONAL_ZONE_DEMO_ACCOUNT_DRIFT" in result.base.reasons


def test_binding_secret_rotation_mid_chain_fails_closed(monkeypatch) -> None:
    bindings = _bindings()
    bindings["halt_control"]["binding_key_marker_sha256"] = "8" * 64

    result = _assemble(monkeypatch, bindings=bindings)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "OPERATIONAL_ZONE_BINDING_SECRET_DRIFT" in result.base.reasons


def test_binding_from_another_run_window_fails_closed(monkeypatch) -> None:
    bindings = _bindings()
    bindings["supervisor"]["observed_at"] = "2026-08-28T15:00:00+00:00"

    result = _assemble(monkeypatch, bindings=bindings)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "OPERATIONAL_ZONE_BINDING_OUTSIDE_SOURCE_RUN:supervisor" in result.base.reasons


def test_missing_or_unexpected_binding_fails_closed(monkeypatch) -> None:
    bindings = _bindings()
    del bindings["recovery_receipt"]

    result = _assemble(monkeypatch, bindings=bindings)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "OPERATIONAL_ZONE_BINDING_MISSING:recovery_receipt" in result.base.reasons


def test_account_binding_is_forbidden_on_db_only_session_and_recovery(monkeypatch) -> None:
    bindings = _bindings()
    bindings["session_start"]["demo_account_binding_present"] = True
    bindings["session_start"]["demo_account_binding_sha256"] = _ACCOUNT_TOKEN

    result = _assemble(monkeypatch, bindings=bindings)

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert "OPERATIONAL_ZONE_UNEXPECTED_ACCOUNT_BINDING:session_start" in result.base.reasons
