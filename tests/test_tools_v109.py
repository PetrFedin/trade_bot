from pathlib import Path

import pytest

from tools.architecture_audit_v109 import audit as architecture_audit
from tools.platform_v109 import live_status, verify_release
from tools.static_audit_v109 import audit as static_audit
from tools.stress_v109 import run_stress

ROOT = Path(__file__).resolve().parents[1]


def test_architecture_static_and_release_identity_pass() -> None:
    assert architecture_audit(ROOT)["status"] == "PASS"
    assert static_audit(ROOT)["status"] == "PASS"
    assert verify_release(ROOT)["status"] == "PASS"


def test_live_status_remains_fail_closed() -> None:
    state = live_status(ROOT)
    assert state["schema"] == 109
    assert state["external_order_routing_allowed"] is False
    assert state["live_trading_allowed"] is False
    assert state["automatic_sign_post_retry_allowed"] is False
    assert state["private_key_material_persisted_by_runtime"] is False


def test_stress_rejects_duplicate_dispatch_and_policy_equivocation() -> None:
    result = run_stress(iterations=100, workers=4)
    assert result["status"] == "PASS"
    assert result["failures"] == 0


def test_stress_rejects_invalid_dimensions() -> None:
    with pytest.raises(ValueError):
        run_stress(iterations=0, workers=1)
