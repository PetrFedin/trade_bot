from __future__ import annotations

import json
from pathlib import Path
import shutil

from tools.architecture_audit_v105 import audit as architecture_audit
from tools.platform_v105 import live_status, main as platform_main, verify_release
from tools.static_audit_v105 import audit as static_audit
from tools.stress_v105 import main as stress_main, run as stress_run


def test_architecture_and_static_audits_pass():
    root = Path(__file__).resolve().parents[1]
    assert architecture_audit(root)["status"] == "PASS"
    assert static_audit(root)["status"] == "PASS"


def test_platform_release_and_live_status_pass():
    root = Path(__file__).resolve().parents[1]
    result = verify_release(root)
    assert result["status"] == "PASS"
    assert result["files_checked"] >= 20
    status = live_status(root)
    assert status["schema"] == 105
    assert status["external_order_routing_allowed"] is False
    assert status["live_trading_allowed"] is False
    assert platform_main(["--root", str(root), "verify-release"]) == 0
    assert platform_main(["--root", str(root), "live-status"]) == 0


def test_platform_detects_missing_and_tampered_files(tmp_path):
    source = Path(__file__).resolve().parents[1]
    root = tmp_path / "copy"
    shutil.copytree(source, root)
    runtime = root / "app/runtime/fleet_operations_v105.py"
    runtime.write_text(runtime.read_text() + "\n# tampered\n")
    missing = root / "README.md"
    missing.unlink()
    result = verify_release(root)
    assert result["status"] == "FAIL"
    assert any(item.startswith("digest:app/runtime") for item in result["findings"])
    assert "missing:README.md" in result["findings"]


def test_stress_small_and_cli_validation():
    result = stress_run(25, 4)
    assert result["status"] == "PASS"
    assert result["unique_tail_digests"] == 25
    assert stress_main(["--iterations", "10", "--workers", "2"]) == 0
