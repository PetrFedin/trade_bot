from __future__ import annotations

import json

import tools.recover_bybit_demo_runtime_lease as recovery_cli


def test_recovery_cli_failure_never_emits_dsn_raw_owner_or_error_message(
    monkeypatch,
    tmp_path,
) -> None:
    dsn = "postgresql://operator:super-secret@db.internal/demo"
    raw_owner = "d" * 64
    monkeypatch.setenv("BYBIT_DEMO_DATABASE_DSN", dsn)

    class _Recovery:
        def __init__(self, observed_dsn: str) -> None:
            assert observed_dsn == dsn

        def inspect(self):
            raise RuntimeError(f"database={dsn}; raw_owner={raw_owner}")

    monkeypatch.setattr(
        recovery_cli,
        "PostgresBybitDemoRuntimeLeaseRecovery",
        _Recovery,
    )
    output = tmp_path / "recovery.json"

    code = recovery_cli.main(
        [
            "--mode",
            "inspect",
            "--output",
            str(output),
        ]
    )

    assert code == 2
    text = output.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["status"] == "RECOVERY_FAILED"
    assert payload["error_type"] == "RuntimeError"
    assert payload["database_identity_exposed"] is False
    assert payload["automatic_recovery_allowed"] is False
    assert payload["automatic_stale_takeover_allowed"] is False
    assert payload["order_writes_supported"] is False
    assert payload["live_mainnet_order_routing_allowed"] is False
    assert dsn not in text
    assert "super-secret" not in text
    assert raw_owner not in text


def test_recovery_cli_success_does_not_echo_operator_reason_or_stop_evidence(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("BYBIT_DEMO_DATABASE_DSN", "postgresql://hidden")
    owner_sha = "a" * 64
    operator_id = "operator-sensitive-label"
    reason = "internal incident details that should remain in durable DB audit"
    stop_evidence = "container-id-sensitive-reference"

    class _Receipt:
        def to_payload(self):
            return {
                "schema": "BYBIT_DEMO_RUNTIME_LEASE_RECOVERY_RECEIPT_V1",
                "status": "RECOVERED",
                "recovery_id": "b" * 64,
                "lease_owner_sha256": owner_sha,
                "control_event_id": "c" * 64,
                "active_checkpoint_present": True,
                "idempotent_existing_recovery": False,
                "immutable_audit": True,
                "automatic_recovery_allowed": False,
                "automatic_stale_takeover_allowed": False,
                "order_writes_supported": False,
                "live_mainnet_order_routing_allowed": False,
            }

    class _Recovery:
        def __init__(self, _dsn: str) -> None:
            pass

        def recover(self, **kwargs):
            assert kwargs["expected_lease_owner_sha256"] == owner_sha
            assert kwargs["operator_id"] == operator_id
            assert kwargs["reason"] == reason
            assert kwargs["process_stop_evidence"] == stop_evidence
            return _Receipt()

    monkeypatch.setattr(
        recovery_cli,
        "PostgresBybitDemoRuntimeLeaseRecovery",
        _Recovery,
    )
    output = tmp_path / "recovery.json"

    code = recovery_cli.main(
        [
            "--mode",
            "recover",
            "--expected-owner-sha256",
            owner_sha,
            "--operator-id",
            operator_id,
            "--reason",
            reason,
            "--process-stop-evidence",
            stop_evidence,
            "--confirmation",
            "RECOVER_BYBIT_DEMO_RUNTIME_LEASE",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    text = output.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["status"] == "RECOVERED"
    assert payload["lease_owner_sha256"] == owner_sha
    assert operator_id not in text
    assert reason not in text
    assert stop_evidence not in text
