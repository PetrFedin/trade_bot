"""ASTRA research console: evidence binding, integrity, accessibility and no demos."""

from __future__ import annotations

import copy
import json

import pytest

from tools import research_ops_console as console

STATUS = json.loads(console.STATUS_PATH.read_text())
REGISTER = json.loads(console.REGISTER_PATH.read_text())
TEMPLATE = console.TEMPLATE_PATH.read_text()


def test_committed_console_exactly_matches_verified_source_and_template():
    assert console.OUTPUT_PATH.read_text() == console.build()


def test_console_deterministic():
    assert console.build() == console.build()


def test_replay_numbers_from_source_and_no_invented_financial_ratios():
    page = console.build()
    for text in ("-176.67", "-17.67%", "35.29%", "53.92%", "10.78%", "22.20%"):
        assert text in page
    assert "Не подтверждены" in page
    assert "Источник заблокирован" in page
    assert "Sharpe" in page
    assert "Доходность к базе" in page
    assert "Нет внешних запросов" in page
    assert "FROZEN · NOT LIVE" in page
    assert "недоступно" not in page.lower() or "NO EXECUTION AUTHORITY" in page
    assert "@@" not in page


def test_all_capabilities_render_and_dependency_links_resolve():
    page = console.build()
    for item in REGISTER["capabilities"]:
        assert f'id="cap-{item["id"]}"' in page
        for dependency in item["depends_on"]:
            assert f'href="#cap-{dependency}"' in page
    assert page.count('<tr id="cap-') == len(REGISTER["capabilities"])
    assert 'id="feature-state"' in page
    assert 'id="feature-search"' in page
    assert 'id="export-features"' in page


def test_offline_user_interface_has_functional_sections_and_no_external_assets():
    page = console.build()
    for section in ("overview", "strategy", "capabilities", "provenance"):
        assert f'id="{section}"' in page
        assert f'href="#{section}"' in page
    assert 'name="viewport"' in page
    assert 'aria-live="polite"' in page
    assert '@media(max-width:430px)' in page
    assert "@media(prefers-reduced-motion:reduce)" in page
    assert "<script src=" not in page
    assert "<link rel=" not in page
    assert "https://" not in page
    assert "fetch(" not in page
    assert "XMLHttpRequest" not in page
    assert "localStorage" not in page
    assert "RECORDED_RESEARCH_ONLY" not in page


def test_any_source_escape_stays_text_not_html():
    status = copy.deepcopy(STATUS)
    status["strategy"]["evidence_source"] = '<img src=x onerror="alert(1)">'
    register = copy.deepcopy(REGISTER)
    register["capabilities"][0]["acceptance_gate"] = '<svg onload="alert(1)">'
    html = console.render(status, register, TEMPLATE, evidence_root=console.ROOT)
    assert '<img src=x' not in html
    assert '<svg onload=' not in html
    assert "&lt;svg" in html
    assert "&lt;img" in html
    assert "<script src=" not in html


def test_unsupported_authority_never_renders_as_live_ready():
    status = copy.deepcopy(STATUS)
    status["trading_authority"]["live_trading_allowed"] = True
    with pytest.raises(ValueError, match="research-only"):
        console.render(status, REGISTER, TEMPLATE, evidence_root=console.ROOT)


def test_missing_template_binding_is_a_hard_error():
    with pytest.raises(console.ResearchConsoleError, match="missing UI placeholder"):
        console.render(STATUS, REGISTER, "<html>invalid template</html>",
                       evidence_root=console.ROOT)


def test_duplicate_source_json_keys_rejected(tmp_path):
    path = tmp_path / "status.json"
    path.write_text('{"schema_version":"one","schema_version":"two"}')
    with pytest.raises(console.ResearchConsoleError, match="duplicate JSON field"):
        console._read_strict(path)


def test_check_mode_fails_when_generated_document_modified(tmp_path, capsys):
    path = tmp_path / "status.html"
    path.write_text("<h1>fake authorized trading</h1>")
    assert console.main(["--check", "--output", str(path)]) == 1
    assert "ASTRA_CONSOLE_STALE_OR_MISSING" in capsys.readouterr().err


def test_check_mode_recognizes_exact_generated_document(tmp_path, capsys):
    path = tmp_path / "status.html"
    assert console.main(["--write", "--output", str(path)]) == 0
    assert console.main(["--check", "--output", str(path)]) == 0
    assert "ASTRA_RESEARCH_CONSOLE_SOURCE_MATCH" in capsys.readouterr().out


def test_no_hidden_demo_symbols_or_lorem_ipsum_copy():
    html = console.build().lower()
    assert "lorem ipsum" not in html
    assert "mock pnl" not in html
    assert "sample returns" not in html
    assert "guaranteed profit" not in html
    assert "demo balance" not in html
