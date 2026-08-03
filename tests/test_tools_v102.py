from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.runtime.sandbox_soak_orchestrator_v102 import FileLeaseStoreV102
from tools.architecture_audit_v102 import audit as architecture_audit
from tools.platform_v102 import main as platform_main
from tools.static_audit_v102 import audit as static_audit
from tools.stress_v102 import main as stress_main

UTC = timezone.utc


def test_audits_pass_on_repository():
    root = Path(__file__).resolve().parents[1]
    assert architecture_audit(root)["status"] == "PASS"
    assert static_audit(root)["status"] == "PASS"


def test_platform_verifies_empty_stores(tmp_path, capsys):
    assert platform_main(["verify-journal", str(tmp_path / "events.jsonl")]) == 0
    assert '"status": "PASS"' in capsys.readouterr().out
    assert platform_main(["verify-archive", str(tmp_path / "archive")]) == 0
    assert '"records": 0' in capsys.readouterr().out
    assert platform_main(["verify-lease", str(tmp_path / "lease.json")]) == 0
    assert '"present": false' in capsys.readouterr().out


def test_platform_verifies_lease(tmp_path, capsys):
    store = FileLeaseStoreV102(tmp_path / "lease.json")
    store.acquire(
        owner_id="operator",
        generation=1,
        now=datetime(2026, 8, 3, tzinfo=UTC),
        ttl=timedelta(minutes=1),
    )
    assert platform_main(["verify-lease", str(store.path)]) == 0
    output = capsys.readouterr().out
    assert '"owner_id": "operator"' in output
    assert '"fencing_token": 1' in output


def test_platform_fails_closed_on_corrupt_lease(tmp_path, capsys):
    path = tmp_path / "lease.json"
    path.write_text("not-json")
    assert platform_main(["verify-lease", str(path)]) == 2
    assert '"status": "FAIL"' in capsys.readouterr().out


def test_stress_tool_small_run(capsys):
    assert stress_main(["--iterations", "20", "--workers", "4"]) == 0
    output = capsys.readouterr().out
    assert '"failures": 0' in output
    assert '"live_trading_allowed": false' in output
