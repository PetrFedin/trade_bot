"""Source-constrained standalone UI acceptance without a trading connector."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.signal_funnel_terminal import (
    TEMPLATE,
    SignalFunnelUIError,
    render,
)


def test_professional_original_source_template_and_keyboard_navigation():
    page = Path(TEMPLATE).read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert 'name="viewport"' in page
    assert "@media(max-width:580px)" in page
    assert ".coin-select:focus-visible" in page
    assert 'selectCoin.type="button"' in page
    assert 'selectCoin.setAttribute("aria-pressed"' in page
    ids = re.findall(r'\bid="([^"]+)"', page)
    assert len(ids) == len(set(ids))
    for control in ("universe", "arm", "ranker", "gate", "cost", "sort"):
        assert control in ids
        assert f'for="{control}"' in page
    for block in (
        "__RESEARCH_DATA__", "__FUNNEL_SHA__", "__BASE_SHA__",
        "__GATE_SHA__", "__START_DATE__", "__END_DATE__",
    ):
        assert page.count(block) == 1


def test_offline_html_makes_no_exchange_or_analytics_network_requests():
    page = Path(TEMPLATE).read_text(encoding="utf-8")
    for invalid in (
        "fetch(", "WebSocket(", "EventSource(", "XMLHttpRequest",
        "http://", "https://", '<script src=', "<iframe",
    ):
        assert invalid not in page
    assert 'href="risk-gate-terminal.html"' in page
    assert 'href="operator-status.html"' in page
    assert 'href="execution-quality-terminal.html"' in page
    assert "PROFITABILITY_NOT_PROVEN" in page
    assert "NO ORDERS" in page
    assert "profit" not in page.lower().split("НЕИЗВЕСТЕН")[-1].split("</main>")[-1]


def test_invalid_source_cannot_create_fake_durable_terminal():
    with pytest.raises(SignalFunnelUIError, match="requalify"):
        render({}, {}, {}, {}, Path("nonexistent-source"))
