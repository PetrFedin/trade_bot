"""Static offline UX and inventory-snapshot regression for ASTRA research UI."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "docs/astra_capability_matrix_2026-10-20.html"
LEDGER = ROOT / "research/competitive_capabilities_2026-10-20.json"


class MatrixPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_source_script = False
        self.payload = []
        self.ids = set()
        self.script_sources = []
        self.forms = 0
        self.has_csp = False

    def handle_starttag(self, tag, attrs):
        pairs = dict(attrs)
        if "id" in pairs:
            self.ids.add(pairs["id"])
        if tag == "script" and pairs.get("id") == "matrix-data":
            assert pairs.get("type") == "application/json"
            self.in_source_script = True
        if tag == "script" and "src" in pairs:
            self.script_sources.append(pairs["src"])
        if tag == "form":
            self.forms += 1
        if tag == "meta" and pairs.get("http-equiv") == "Content-Security-Policy":
            csp = pairs.get("content", "")
            self.has_csp = "connect-src 'none'" in csp and "form-action 'none'" in csp

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_source_script = False

    def handle_data(self, data):
        if self.in_source_script:
            self.payload.append(data)


def test_dashboard_embeds_exact_validated_research_snapshot():
    page = UI.read_text(encoding="utf-8")
    parsed = MatrixPage()
    parsed.feed(page)
    embedded = json.loads("".join(parsed.payload))
    source = json.loads(LEDGER.read_text(encoding="utf-8"))
    assert embedded == source
    assert embedded["authority"]["live_trading_allowed"] is False
    assert embedded["authority"]["strategy_promotion_allowed"] is False
    assert embedded["authority"]["profitability_proven"] is False
    assert len(embedded["capabilities"]) == 31
    assert len(embedded["competitors"]) == 11


def test_dashboard_has_accessible_live_filters_and_reset():
    page = UI.read_text(encoding="utf-8")
    parsed = MatrixPage()
    parsed.feed(page)
    assert {"main", "matrix-data", "capabilities", "query", "status",
            "priority", "area", "reset", "empty", "results"}.issubset(parsed.ids)
    assert 'aria-live="polite"' in page
    assert "addEventListener" in page
    assert ".capability[hidden],.empty[hidden]{display:none !important}" in page
    assert "@media(max-width:650px)" in page
    assert "<details" not in page or "Критерии, источники и связи" in page


def test_dashboard_does_not_offer_execution_or_external_code():
    page = UI.read_text(encoding="utf-8")
    parsed = MatrixPage()
    parsed.feed(page)
    assert parsed.script_sources == []
    assert parsed.forms == 0
    assert parsed.has_csp is True
    assert "fetch(" not in page
    assert "XMLHttpRequest" not in page
    assert "innerHTML" not in page
    assert "a.rel=\"noopener noreferrer\"" in page
    assert "Не торговый терминал" in page
    assert "PROFITABILITY NOT PROVEN" in page
