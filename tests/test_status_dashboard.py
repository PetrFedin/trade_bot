from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import status_dashboard

SOURCE = json.loads(status_dashboard.STATUS_PATH.read_text())


def test_committed_console_agrees_with_its_source() -> None:
    """The console is generated; a hand edit or a status change must be caught."""
    assert status_dashboard.OUTPUT_PATH.exists()
    assert status_dashboard.OUTPUT_PATH.read_text() == status_dashboard.render(SOURCE)


def test_render_is_deterministic() -> None:
    assert status_dashboard.render(SOURCE) == status_dashboard.render(SOURCE)


def test_closed_authority_renders_as_closed() -> None:
    document = status_dashboard.render(SOURCE)
    assert document.count('class="pill shut"') == len(status_dashboard.AUTHORITIES)
    assert 'class="pill open"' not in document


def test_granted_authority_renders_as_granted() -> None:
    """A future promotion must show as granted rather than silently stay red."""
    status = json.loads(json.dumps(SOURCE))
    status["trading_authority"]["demo_trading_allowed"] = True
    document = status_dashboard.render(status)
    assert document.count('class="pill open"') == 1
    assert document.count('class="pill shut"') == len(status_dashboard.AUTHORITIES) - 1


def test_negative_replay_numbers_are_rendered_verbatim() -> None:
    replay = SOURCE["strategy"]["latest_frozen_bybit_price_only_replay"]
    document = status_dashboard.render(SOURCE)
    assert str(replay["net_pnl_usdt"]) in document
    assert str(replay["losses"]) in document
    assert SOURCE["strategy"]["status"] in document


def test_render_escapes_source_text() -> None:
    status = json.loads(json.dumps(SOURCE))
    status["trading_authority"]["reason"] = '<script>alert("x")</script>'
    document = status_dashboard.render(status)
    assert "<script>alert" not in document
    assert "&lt;script&gt;" in document


def test_check_reports_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "console.html"
    output.write_text("stale")
    code = status_dashboard.main(["--check", "--output", str(output)])
    assert code == 1
    assert "CONSOLE_ARTIFACT_STALE" in capsys.readouterr().err


def test_check_reports_missing_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = status_dashboard.main(["--check", "--output", str(tmp_path / "absent.html")])
    assert code == 1
    assert "CONSOLE_MISSING" in capsys.readouterr().err


def test_write_then_check_round_trips(tmp_path: Path) -> None:
    output = tmp_path / "console.html"
    assert status_dashboard.main(["--write", "--output", str(output)]) == 0
    assert status_dashboard.main(["--check", "--output", str(output)]) == 0
