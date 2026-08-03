from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

from app.runtime.sandbox_qualification_v101 import (
    Approval,
    ApprovalKey,
    EventStore,
    EventType,
    KillSwitchStore,
    Side,
    State,
)
from tools.architecture_audit_v101 import audit as architecture_audit
from tools.platform_v101 import main as platform_main
from tools.static_audit_v101 import audit as static_audit
from tools.stress_v101 import main as stress_main

UTC = timezone.utc
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


def test_audits_pass_on_repository():
    assert architecture_audit(ROOT)["status"] == "PASS"
    assert static_audit(ROOT)["status"] == "PASS"


def test_static_audit_detects_forbidden_tls(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "app" / "bad.py").write_text("verify=False\n")
    result = static_audit(tmp_path)
    assert result["status"] == "FAIL"
    assert result["findings"]


def test_verify_journal_and_kill_status_cli(tmp_path, capsys):
    journal = tmp_path / "events.jsonl"
    EventStore(journal).append(
        qualification_id="q",
        event_type=EventType.PROBE_STARTED,
        from_state=State.CREATED,
        to_state=State.PROBING,
        occurred_at=NOW,
        generation=1,
        attributes={},
    )
    assert platform_main(["verify-journal", str(journal)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"

    kill = tmp_path / "kill.json"
    KillSwitchStore(kill).engage(reason="test", now=NOW, generation=1)
    assert platform_main(["kill-status", str(kill)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["engaged"] is True
    assert output["live_trading_allowed"] is False


def test_seal_and_verify_approval_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ApprovalKey.ENV, "x" * 40)
    source = tmp_path / "approval.json"
    output = tmp_path / "sealed.json"
    source.write_text(json.dumps({
        "approval_id": "approval-1",
        "operator_id": "operator-a",
        "nonce": "nonce-0123456789abcdef",
        "generation": 7,
        "account_id": "acct-1",
        "symbol": "AAPL",
        "side": "BUY",
        "maximum_quantity": "1",
        "maximum_notional": "100",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "allow_paper_mutations": True
    }))
    assert platform_main(["seal-approval", str(source), str(output)]) == 0
    capsys.readouterr()
    sealed = json.loads(output.read_text())
    assert len(sealed["signature"]) == 64
    assert "x" * 20 not in output.read_text()
    assert platform_main(["verify-approval", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_stress_tool_small(capsys):
    assert stress_main(["--iterations", "8", "--workers", "2"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "PASS"
    assert result["failures"] == 0


def test_migration_is_packaged_verbatim():
    source = (ROOT / "migrations/v101/001_external_sandbox_qualification.sql").read_text()
    packaged = (ROOT / "app/platform_assets/v101/migrations/001_external_sandbox_qualification.sql").read_text()
    assert source == packaged
    assert "external_order_routing_allowed = false" in source
    assert "append-only" in source
