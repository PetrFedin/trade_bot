from __future__ import annotations

import json
from pathlib import Path

from tools.architecture_audit_v100 import audit as architecture_audit
from tools.platform_v100 import main as platform_main
from tools.static_audit_v100 import audit as static_audit


def test_architecture_and_static_audits_pass() -> None:
    root = Path(__file__).resolve().parents[1]
    assert architecture_audit(root) == ()
    assert static_audit(root) == ()


def test_credentials_status_redacts_secret(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ASTRA_ALPACA_PAPER_KEY_ID", "paper-key")
    monkeypatch.setenv("ASTRA_ALPACA_PAPER_SECRET_KEY", "paper-secret")
    assert platform_main(["credentials-status"]) == 0
    output = capsys.readouterr().out
    document = json.loads(output)
    assert document["credentials_configured"] is True
    assert "paper-secret" not in output
    assert document["live_trading_allowed"] is False


def test_stress_v100_is_deterministically_safe() -> None:
    from tools.stress_v100 import run

    report = run(iterations=100, workers=4)
    assert report["accepted_updates"] == 100
    assert report["ready"] is True
    assert report["external_order_routing_allowed"] is False
    assert report["live_trading_allowed"] is False
