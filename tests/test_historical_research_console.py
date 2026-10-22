"""Read-only research console regression tests, using explicitly synthetic fixtures."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import tools.historical_research_console as ui
from tests.test_historical_execution_journal import (
    report,
    synthetic_auditor_receipt,
)
from tools.historical_execution_journal import build_journal


@pytest.fixture(scope="module")
def valid_research():
    original = report()
    auditor = synthetic_auditor_receipt(original)
    journal = build_journal(original, auditor)
    return journal, original, auditor


def test_offline_terminal_is_a_complete_responsive_document(valid_research):
    html = ui.render(*valid_research)
    assert html.startswith("<!doctype html>")
    assert html.endswith("</body></html>\n")
    assert '<html lang="ru">' in html
    assert "width=device-width" in html
    assert "@media(max-width:620px)" in html
    assert "REUSED_INSPECTED_HISTORY" in html
    assert "NOT FOR ORDER EXECUTION" in html
    assert "READ ONLY / PROFITABILITY_NOT_PROVEN" in html
    assert "—" in html
    assert "http://" not in html
    assert "https://" not in html
    assert "fetch(" not in html
    assert "WebSocket(" not in html
    assert "<script src=" not in html
    assert 'id="return">0.100%' in html


def test_exact_36_scenarios_and_independent_auditor_lineage(valid_research):
    journal, original, auditor = valid_research
    payload = ui._verified_payload(journal, original, auditor)
    assert len(payload["cases"]) == 36
    assert len(payload["pairs"]) == 12
    assert payload["trading_allowed"] is False
    assert payload["journal_sha256"] == journal["journal_sha256"]
    assert payload["auditor_sha256"] == journal["independent_auditor_sha256"]
    assert payload["cases"][ui.DEFAULT_KEY]["trades"] == 1
    assert all("windows" in item for item in payload["cases"].values())


def test_rehashed_unverified_console_cannot_impersonate_original(valid_research):
    journal, original, auditor = valid_research
    altered = copy.deepcopy(journal)
    altered["profitability_proven"] = True
    with pytest.raises(ui.ResearchConsoleError, match="diverges"):
        ui.render(altered, original, auditor)


def test_source_cannot_be_swapped_without_independent_money_audit(valid_research):
    journal, original, auditor = valid_research
    bad = copy.deepcopy(original)
    bad["comparisons"][0]["models"]["DAILY_STOP_FIRST:NO_FUNDING"]["metrics"][
        "ending_equity_usd"
    ] = "9999"
    with pytest.raises(ValueError):
        ui.render(journal, bad, auditor)


def test_json_source_must_never_close_own_script_tag():
    malicious = "</script><script>window.trading_enabled=true</script>&\u2028"
    safe = ui._safe_json({"a": malicious})
    assert "</script>" not in safe
    assert "<script>" not in safe
    assert "\\u003c" in safe
    assert "\\u0026" in safe
    assert json.loads(safe) == {"a": malicious}


def test_reject_invalid_numeric_values():
    for value in (True, "nan", "Infinity", None):
        with pytest.raises(ui.ResearchConsoleError):
            ui._number(value)


def test_atomic_console_creation_and_fails_on_second_attempt(tmp_path):
    destination = tmp_path / "private" / "terminal.html"
    ui.publish_new_html(destination, "<!doctype html><title>proof</title>")
    assert destination.read_text() == "<!doctype html><title>proof</title>"
    with pytest.raises(ui.ResearchConsoleError, match="no replacement"):
        ui.publish_new_html(destination, "tamper")
    assert destination.read_text() == "<!doctype html><title>proof</title>"
    assert not list(destination.parent.glob(".astra-html-*"))


def test_existing_symlink_output_and_symlinked_parent_fail_closed(tmp_path):
    protected = tmp_path / "protected.html"
    protected.write_text("preserved")
    target = tmp_path / "terminal.html"
    target.symlink_to(protected)
    with pytest.raises(ui.ResearchConsoleError, match="no replacement"):
        ui.publish_new_html(target, "<div>different</div>")
    assert protected.read_text() == "preserved"
    linked_dir = tmp_path / "alias"
    linked_dir.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ui.ResearchConsoleError, match="symlink"):
        ui.publish_new_html(linked_dir / "other.html", "bad")


def test_concurrent_writer_cannot_be_overwritten(tmp_path, monkeypatch):
    destination = tmp_path / "terminal.html"

    def interfering_link(source, target, *, follow_symlinks):
        assert follow_symlinks is False
        Path(target).write_text("competitor", encoding="utf-8")
        raise FileExistsError("racing writer")

    monkeypatch.setattr(ui.os, "link", interfering_link)
    with pytest.raises(ui.ResearchConsoleError, match="atomic"):
        ui.publish_new_html(destination, "historical metrics")
    assert destination.read_text() == "competitor"
    assert not list(tmp_path.glob(".astra-html-*"))


def test_identical_input_renders_identical_document(valid_research):
    assert ui.render(*valid_research) == ui.render(*valid_research)


def test_models_without_funding_still_do_not_enable_order_routes(valid_research):
    html = ui.render(*valid_research)
    assert 'value="NO_FUNDING"' in html
    assert '"trading_allowed":false' in html
    assert 'value="DAILY_STOP_FIRST"' in html
    assert 'value="HOURLY_COLLISION_REFINED"' in html
