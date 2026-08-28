from __future__ import annotations

import hashlib

import app.execution.bybit_demo_operational_release_checkpoint_binding as binding
from app.execution.bybit_demo_operational_release_evidence import (
    BybitDemoOperationalReleaseEvidence,
    BybitDemoOperationalReleaseStage,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _proven_result() -> BybitDemoOperationalReleaseEvidence:
    return BybitDemoOperationalReleaseEvidence(
        stage=BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN,
        reasons=(),
        git_sha="a" * 40,
        evidence_sha256={},
        source_runs={},
        source_run_metadata_sha256="f" * 64,
        next_required_evidence=None,
        release_gate_complete=True,
    )


def test_exact_entry_checkpoint_identity_is_accepted() -> None:
    entry = {"entry_order_link_id": "ASTRA-DEMO-E-EXACT-ENTRY"}
    recovery = {
        "active_checkpoint_present": True,
        "active_checkpoint_entry_order_link_id_sha256": _digest(
            "ASTRA-DEMO-E-EXACT-ENTRY"
        ),
    }

    assert (
        binding._checkpoint_binding_failure_reason(
            operational_entry=entry,
            recovery_receipt=recovery,
        )
        is None
    )


def test_recovery_of_another_checkpoint_fails_closed() -> None:
    entry = {"entry_order_link_id": "ASTRA-DEMO-E-APPROVED"}
    recovery = {
        "active_checkpoint_present": True,
        "active_checkpoint_entry_order_link_id_sha256": _digest(
            "ASTRA-DEMO-E-OTHER"
        ),
    }

    assert binding._checkpoint_binding_failure_reason(
        operational_entry=entry,
        recovery_receipt=recovery,
    ) == "RECOVERY_DRILL_ENTRY_CHECKPOINT_MISMATCH"


def test_recovery_without_active_checkpoint_is_not_full_entry_drill_proof() -> None:
    entry = {"entry_order_link_id": "ASTRA-DEMO-E-APPROVED"}
    recovery = {
        "active_checkpoint_present": False,
        "active_checkpoint_entry_order_link_id_sha256": None,
    }

    assert binding._checkpoint_binding_failure_reason(
        operational_entry=entry,
        recovery_receipt=recovery,
    ) == "RECOVERY_DRILL_ACTIVE_CHECKPOINT_NOT_PROVEN"


def test_invalid_checkpoint_digest_fails_closed() -> None:
    entry = {"entry_order_link_id": "ASTRA-DEMO-E-APPROVED"}
    recovery = {
        "active_checkpoint_present": True,
        "active_checkpoint_entry_order_link_id_sha256": "not-a-sha256",
    }

    assert binding._checkpoint_binding_failure_reason(
        operational_entry=entry,
        recovery_receipt=recovery,
    ) == "RECOVERY_DRILL_CHECKPOINT_IDENTITY_INVALID"


def test_checkpoint_mismatch_demotes_complete_manifest_to_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        binding,
        "assemble_bybit_demo_operational_release_evidence",
        lambda **_kwargs: _proven_result(),
    )
    entry = {"entry_order_link_id": "ASTRA-DEMO-E-APPROVED"}
    recovery = {
        "active_checkpoint_present": True,
        "active_checkpoint_entry_order_link_id_sha256": _digest(
            "ASTRA-DEMO-E-DIFFERENT"
        ),
    }

    result = binding.assemble_checkpoint_bound_bybit_demo_operational_release_evidence(
        git_sha="a" * 40,
        activation_readiness={},
        evidence_sha256={},
        source_run_metadata={},
        source_run_metadata_sha256="f" * 64,
        operational_entry=entry,
        recovery_receipt=recovery,
    )

    assert result.stage is BybitDemoOperationalReleaseStage.BLOCKED
    assert result.release_gate_complete is False
    assert result.next_required_evidence is None
    assert result.reasons == ("RECOVERY_DRILL_ENTRY_CHECKPOINT_MISMATCH",)


def test_checkpoint_match_preserves_complete_manifest(monkeypatch) -> None:
    proven = _proven_result()
    monkeypatch.setattr(
        binding,
        "assemble_bybit_demo_operational_release_evidence",
        lambda **_kwargs: proven,
    )
    entry_order_link_id = "ASTRA-DEMO-E-APPROVED"

    result = binding.assemble_checkpoint_bound_bybit_demo_operational_release_evidence(
        git_sha="a" * 40,
        activation_readiness={},
        evidence_sha256={},
        source_run_metadata={},
        source_run_metadata_sha256="f" * 64,
        operational_entry={"entry_order_link_id": entry_order_link_id},
        recovery_receipt={
            "active_checkpoint_present": True,
            "active_checkpoint_entry_order_link_id_sha256": _digest(
                entry_order_link_id
            ),
        },
    )

    assert result is proven
    assert result.stage is BybitDemoOperationalReleaseStage.RECOVERY_DRILL_PROVEN
    assert result.release_gate_complete is True
