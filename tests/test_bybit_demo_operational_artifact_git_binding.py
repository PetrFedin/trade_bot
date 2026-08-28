from __future__ import annotations

import json
import sys
from pathlib import Path

import tools.control_bybit_demo_entries as control_cli
import tools.manage_bybit_demo_session_risk as session_cli
import tools.recover_bybit_demo_runtime_lease as recovery_cli
import tools.run_bybit_demo_operator_approved_entry as entry_cli
import tools.run_bybit_demo_persistent_supervisor as supervisor_cli

_GIT_SHA = "c" * 40


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_supervisor_artifact_binds_github_sha(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_SHA", _GIT_SHA)
    target = tmp_path / "supervisor.json"

    supervisor_cli._emit({"schema": "TEST"}, output=target)

    assert _read(target)["git_sha"] == _GIT_SHA


def test_entry_artifact_binds_github_sha(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_SHA", _GIT_SHA)
    target = tmp_path / "entry.json"

    entry_cli._emit({"schema": "TEST"}, output=target)

    assert _read(target)["git_sha"] == _GIT_SHA


def test_recovery_artifact_binds_github_sha(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GITHUB_SHA", _GIT_SHA)
    target = tmp_path / "recovery.json"

    recovery_cli._emit(str(target), {"schema": "TEST"})

    assert _read(target)["git_sha"] == _GIT_SHA


def test_control_failure_artifact_binds_explicit_git_sha(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "control.json"
    monkeypatch.delenv("BYBIT_DEMO_DATABASE_DSN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "control_bybit_demo_entries",
            "--mode",
            "status",
            "--git-sha",
            _GIT_SHA,
            "--output",
            str(target),
        ],
    )

    code = control_cli.main()

    assert code == 2
    payload = _read(target)
    assert payload["git_sha"] == _GIT_SHA
    assert payload["status"] == "CONTROL_OPERATION_FAILED"


def test_session_failure_artifact_binds_explicit_git_sha(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "session.json"
    monkeypatch.delenv("BYBIT_DEMO_DATABASE_DSN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "manage_bybit_demo_session_risk",
            "--mode",
            "status",
            "--git-sha",
            _GIT_SHA,
            "--output",
            str(target),
        ],
    )

    code = session_cli.main()

    assert code == 2
    payload = _read(target)
    assert payload["git_sha"] == _GIT_SHA
    assert payload["status"] == "SESSION_OPERATION_FAILED"


def test_missing_github_sha_is_explicitly_unbound(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    target = tmp_path / "entry.json"

    entry_cli._emit({"schema": "TEST"}, output=target)

    assert _read(target)["git_sha"] is None
