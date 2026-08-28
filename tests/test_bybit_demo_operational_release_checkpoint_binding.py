from __future__ import annotations

import hashlib

from app.execution.bybit_demo_operational_release_checkpoint_binding import (
    _checkpoint_binding_failure_reason,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_exact_entry_checkpoint_identity_is_accepted() -> None:
    entry = {"entry_order_link_id": "ASTRA-DEMO-E-EXACT-ENTRY"}
    recovery = {
        "active_checkpoint_present": True,
        "active_checkpoint_entry_order_link_id_sha256": _digest(
            "ASTRA-DEMO-E-EXACT-ENTRY"
        ),
    }

    assert (
        _checkpoint_binding_failure_reason(
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

    assert _checkpoint_binding_failure_reason(
        operational_entry=entry,
        recovery_receipt=recovery,
    ) == "RECOVERY_DRILL_ENTRY_CHECKPOINT_MISMATCH"


def test_recovery_without_active_checkpoint_is_not_full_entry_drill_proof() -> None:
    entry = {"entry_order_link_id": "ASTRA-DEMO-E-APPROVED"}
    recovery = {
        "active_checkpoint_present": False,
        "active_checkpoint_entry_order_link_id_sha256": None,
    }

    assert _checkpoint_binding_failure_reason(
        operational_entry=entry,
        recovery_receipt=recovery,
    ) == "RECOVERY_DRILL_ACTIVE_CHECKPOINT_NOT_PROVEN"


def test_invalid_checkpoint_digest_fails_closed() -> None:
    entry = {"entry_order_link_id": "ASTRA-DEMO-E-APPROVED"}
    recovery = {
        "active_checkpoint_present": True,
        "active_checkpoint_entry_order_link_id_sha256": "not-a-sha256",
    }

    assert _checkpoint_binding_failure_reason(
        operational_entry=entry,
        recovery_receipt=recovery,
    ) == "RECOVERY_DRILL_CHECKPOINT_IDENTITY_INVALID"
