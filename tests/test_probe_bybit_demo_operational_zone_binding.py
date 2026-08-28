from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import tools.probe_bybit_demo_operational_zone_binding as cli


def test_database_only_cli_writes_sanitized_sidecar(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BYBIT_DEMO_ZONE_BINDING_SECRET", "x" * 40)
    monkeypatch.setenv(
        "BYBIT_DEMO_DATABASE_DSN",
        "postgresql://operator:super-secret@db.example.com/astra_demo",
    )
    monkeypatch.setattr(
        cli,
        "build_bybit_demo_operational_zone_binding",
        lambda **kwargs: SimpleNamespace(
            to_payload=lambda: {
                "schema": "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V1",
                "status": "BOUND",
                "passed": True,
                "producer": kwargs["producer"],
                "git_sha": kwargs["git_sha"],
                "observed_at": datetime(2026, 8, 28, 12, 0, tzinfo=UTC).isoformat(),
                "binding_algorithm": "HMAC-SHA256",
                "binding_key_marker_sha256": "1" * 64,
                "database_binding_present": True,
                "database_binding_sha256": "2" * 64,
                "demo_account_binding_present": False,
                "demo_account_binding_sha256": None,
                "order_writes_supported": False,
                "live_mainnet_order_routing_allowed": False,
            }
        ),
    )
    output = tmp_path / "zone.json"

    code = cli.main(
        [
            "--producer",
            "session_start",
            "--git-sha",
            "a" * 40,
            "--output",
            str(output),
        ]
    )

    assert code == 0
    text = output.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["producer"] == "session_start"
    assert payload["database_binding_present"] is True
    assert "super-secret" not in text
    assert "db.example.com" not in text
    assert "operator" not in text


def test_failure_payload_never_exposes_exception_message(monkeypatch, tmp_path) -> None:
    secret_marker = "postgresql://operator:super-secret@db.example.com/astra_demo"
    monkeypatch.setenv("BYBIT_DEMO_ZONE_BINDING_SECRET", "x" * 40)
    monkeypatch.setenv("BYBIT_DEMO_DATABASE_DSN", secret_marker)

    def _fail(**_kwargs):
        raise RuntimeError(f"cannot bind {secret_marker}")

    monkeypatch.setattr(cli, "build_bybit_demo_operational_zone_binding", _fail)
    output = tmp_path / "zone.json"

    code = cli.main(
        [
            "--producer",
            "recovery_receipt",
            "--git-sha",
            "a" * 40,
            "--output",
            str(output),
        ]
    )

    assert code == 2
    text = output.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["status"] == "BLOCKED"
    assert payload["error_type"] == "RuntimeError"
    assert "super-secret" not in text
    assert "db.example.com" not in text
