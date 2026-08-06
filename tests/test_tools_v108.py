from pathlib import Path

import pytest

from tools.architecture_audit_v108 import audit as architecture_audit
from tools.static_audit_v108 import audit as static_audit
from tools.stress_v108 import run_stress

ROOT = Path(__file__).resolve().parents[1]


def test_architecture_and_static_audits_pass() -> None:
    assert architecture_audit(ROOT)["status"] == "PASS"
    assert static_audit(ROOT)["status"] == "PASS"


def test_stress_is_deterministic_and_rejects_replay() -> None:
    result = run_stress(iterations=100, workers=4)
    assert result["status"] == "PASS"
    assert result["ledger_size"] == 300
    assert result["replay_rejected"] is True


def test_stress_rejects_invalid_dimensions() -> None:
    with pytest.raises(ValueError):
        run_stress(iterations=0, workers=1)
