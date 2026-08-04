from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from tools.architecture_audit_v106 import audit as architecture_audit
from tools.platform_v106 import live_status, main as platform_main, verify_release
from tools.static_audit_v106 import audit as static_audit
from tools.stress_v106 import run_stress

ROOT = Path(__file__).resolve().parents[1]


def test_architecture_and_static_audits_pass():
    architecture = architecture_audit(ROOT)
    security = static_audit(ROOT)
    assert architecture["status"] == "PASS", architecture
    assert security["status"] == "PASS", security


def test_platform_live_status_and_release_verification(capsys):
    status = live_status(ROOT)
    assert status["status"] == "LOCAL_QUALIFICATION_ONLY"
    assert status["kubernetes_mutations_allowed"] is False
    assert status["external_order_routing_allowed"] is False
    assert status["live_trading_allowed"] is False
    assert verify_release(ROOT)["status"] == "PASS"
    assert platform_main(["--root", str(ROOT), "live-status"]) == 0
    assert "LOCAL_QUALIFICATION_ONLY" in capsys.readouterr().out
    assert platform_main(["--root", str(ROOT), "verify-release"]) == 0
    assert '"status": "PASS"' in capsys.readouterr().out


def test_release_verification_detects_missing_identity(tmp_path):
    assert verify_release(tmp_path)["status"] == "FAIL"


def test_release_verification_detects_tampered_file(tmp_path):
    shutil.copytree(ROOT, tmp_path / "repo")
    target = tmp_path / "repo"
    runtime = target / "app/runtime/deployment_qualification_v106.py"
    runtime.write_text(runtime.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    result = verify_release(target)
    assert result["status"] == "FAIL"
    assert "digest:app/runtime/deployment_qualification_v106.py" in result["findings"]
    assert platform_main(["--root", str(target), "verify-release"]) == 2


def test_stress_is_deterministic_and_unique():
    result = run_stress(200, 8)
    assert result["status"] == "PASS"
    assert result["failures"] == 0
    assert result["unique_tail_digests"] == 200
    assert result["replay_ledger_size"] == 200


@pytest.mark.parametrize("iterations,workers", [(0, 1), (1, 0), (-1, 8)])
def test_stress_rejects_invalid_inputs(iterations, workers):
    with pytest.raises(ValueError):
        run_stress(iterations, workers)


def test_static_audit_detects_forbidden_transport_mutation(tmp_path):
    shutil.copytree(ROOT, tmp_path / "repo")
    target = tmp_path / "repo"
    runtime = target / "app/runtime/fake_v106.py"
    runtime.write_text('method="POST"\n', encoding="utf-8")
    result = static_audit(target)
    assert result["status"] == "FAIL"
    assert any("method=\"POST\"" in finding for finding in result["findings"])


def test_architecture_audit_detects_missing_file(tmp_path):
    shutil.copytree(ROOT, tmp_path / "repo")
    target = tmp_path / "repo"
    (target / "app/runtime/kubernetes_qualification_adapter_v106.py").unlink()
    result = architecture_audit(target)
    assert result["status"] == "FAIL"
    assert any("missing:app/runtime/kubernetes_qualification_adapter_v106.py" == finding for finding in result["findings"])
