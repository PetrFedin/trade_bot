from __future__ import annotations

from pathlib import Path

_WORKFLOW = Path(".github/workflows/bybit-demo-operational-release-evidence.yml")


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def test_release_evidence_workflow_is_read_only_and_unprivileged() -> None:
    text = _text()

    assert "contents: read" in text
    assert "actions: read" in text
    assert "runs-on: ubuntu-latest" in text
    assert "self-hosted" not in text
    assert "environment: bybit-demo" not in text
    assert "secrets." not in text
    assert "BYBIT_DEMO_DATABASE_DSN" not in text
    assert "BYBIT_DEMO_TRADING_API_KEY" not in text
    assert "BYBIT_DEMO_READONLY_API_KEY" not in text


def test_release_evidence_workflow_requires_exact_manual_successful_sources() -> None:
    text = _text()

    for workflow_name in (
        "bybit-demo-activation-readiness",
        "bybit-demo-session-start",
        "bybit-demo-persistent-supervisor",
        "bybit-demo-control-plane",
        "bybit-operator-approved-demo-execution",
        "bybit-demo-runtime-lease-recovery",
    ):
        assert workflow_name in text
    assert 'run.get("event") != "workflow_dispatch"' in text
    assert 'run.get("status") != "completed"' in text
    assert 'run.get("conclusion") != "success"' in text
    assert 'run.get("head_sha") != exact_sha' in text
    assert 'run.get("updated_at")' in text
    assert '"run_completed_at": completed_at' in text
    assert "non-contiguous operational evidence input" in text


def test_release_evidence_workflow_downloads_exact_artifacts_and_fails_on_missing_output() -> None:
    text = _text()

    assert 'gh run download "$run_id"' in text
    assert '"bybit-demo-activation-readiness-$READINESS_RUN_ID"' in text
    assert '"bybit-demo-persistent-supervisor-$SUPERVISOR_RUN_ID"' in text
    assert '"bybit-demo-control-plane-$ARM_RUN_ID"' in text
    assert '"bybit-demo-operational-entry-$ENTRY_RUN_ID"' in text
    assert '"bybit-demo-runtime-lease-recovery-$RECOVERY_RUN_ID"' in text
    assert "bybit-demo-session-(status|initialize)-$SESSION_RUN_ID" in text
    assert "--arm-control evidence/arm/bybit-demo-control-plane.json" in text
    assert "python -m tools.assemble_bybit_demo_operational_release_evidence" in text
    assert "if-no-files-found: error" in text
    assert "retention-days: 30" in text
