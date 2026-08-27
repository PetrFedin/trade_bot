from __future__ import annotations

from pathlib import Path

_WORKFLOW = Path(".github/workflows/bybit-operator-approved-demo-execution.yml")


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _dispatch_inputs(text: str) -> str:
    return text.split("  workflow_dispatch:\n", 1)[1].split("\npermissions:\n", 1)[0]


def _operational_job(text: str) -> str:
    return text.split("\n  operational-entry:\n", 1)[1]


def test_dispatch_exposes_only_exact_operator_identity_inputs() -> None:
    dispatch = _dispatch_inputs(_text())

    assert "      evidence_rank:" in dispatch
    assert "      symbol:" in dispatch
    assert "      confirmation_phrase:" in dispatch
    for forbidden in (
        "      side:",
        "      quantity:",
        "      risk:",
        "      notional:",
        "      fallback:",
        "      ttl_seconds:",
        "      broker_host:",
        "      writes_enabled:",
    ):
        assert forbidden not in dispatch


def test_operational_job_is_protected_fixed_egress_and_non_cancellable() -> None:
    job = _operational_job(_text())

    assert "if: github.event_name == 'workflow_dispatch'" in job
    assert "runs-on: [self-hosted, bybit-demo]" in job
    assert "environment: bybit-demo" in job
    assert "group: bybit-demo-protected-operational-entry" in job
    assert "cancel-in-progress: false" in job
    assert "ubuntu-latest" not in job


def test_operational_job_has_one_runner_call_and_no_arm_or_mainnet_write_path() -> None:
    job = _operational_job(_text())

    runner = "python -m tools.run_bybit_demo_operator_approved_entry"
    assert job.count(runner) == 1
    assert "arm_new_entries" not in job
    assert "tools.run_bybit_demo_control" not in job
    assert "BYBIT_MAINNET_API_KEY" not in job
    assert "BYBIT_MAINNET_API_SECRET" not in job
    assert "BYBIT_MAINNET_TRADING" not in job
    assert "BYBIT_MAINNET_READONLY_API_KEY_SHA256" in job
    assert "--fallback" not in job
    assert "--quantity" not in job
    assert "--risk" not in job
    assert "--ttl" not in job


def test_operational_evidence_is_uploaded_even_when_runner_blocks() -> None:
    job = _operational_job(_text())

    assert "Upload allowlisted operational entry evidence" in job
    assert "if: always()" in job
    assert "artifacts/bybit-demo-operational-entry.json" in job
