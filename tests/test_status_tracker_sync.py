from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import status_tracker_sync


def write_status(tmp_path: Path, gate: object) -> Path:
    path = tmp_path / "CURRENT_SYSTEM_STATUS.json"
    path.write_text(json.dumps({"engineering_qualification": {"next_vertical_gate": gate}}))
    return path


def test_declared_gate_returns_the_declared_block(tmp_path: Path) -> None:
    path = write_status(tmp_path, {"issue": 142, "name": "CANONICAL_LIVE_MARKET_DATA"})
    assert status_tracker_sync.declared_gate(path) == {
        "issue": 142,
        "name": "CANONICAL_LIVE_MARKET_DATA",
    }


def test_declared_gate_rejects_missing_qualification(tmp_path: Path) -> None:
    path = tmp_path / "CURRENT_SYSTEM_STATUS.json"
    path.write_text(json.dumps({"release": {}}))
    with pytest.raises(SystemExit, match="engineering_qualification is missing"):
        status_tracker_sync.declared_gate(path)


def test_declared_gate_rejects_gate_without_issue(tmp_path: Path) -> None:
    path = write_status(tmp_path, {"name": "NO_ISSUE"})
    with pytest.raises(SystemExit, match="next_vertical_gate.issue is missing"):
        status_tracker_sync.declared_gate(path)


def test_open_gate_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_status(tmp_path, {"issue": 142, "name": "CANONICAL_LIVE_MARKET_DATA"})
    monkeypatch.setattr(
        status_tracker_sync, "issue_state", lambda *_args, **_kwargs: ("open", "P0: ingest")
    )
    assert status_tracker_sync.main(["--status-path", str(path)]) == 0


def test_closed_gate_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_status(tmp_path, {"issue": 138, "name": "RESIDUAL_EXECUTION_OMS_INTEGRITY"})
    monkeypatch.setattr(
        status_tracker_sync, "issue_state", lambda *_args, **_kwargs: ("closed", "P0: converge")
    )
    assert status_tracker_sync.main(["--status-path", str(path)]) == 1
    assert "STALE_VERTICAL_GATE" in capsys.readouterr().err


def test_committed_status_declares_a_gate() -> None:
    """The real status surface must always declare a gate for the check to verify."""
    gate = status_tracker_sync.declared_gate(status_tracker_sync.STATUS_PATH)
    assert isinstance(gate["issue"], int)
