"""Read-only HTML evidence checks, from genuine retained coin and public data."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.depth_execution_research import build_depth_stress
from tools.execution_quality_console import ExecutionConsoleError, render
from tools.independent_coin_strategy_lab import BROAD21, study

BARS = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
BOOK = Path("research/snapshots/bybit_public_depth_observation_20261023.json")


@pytest.fixture(scope="module")
def originals():
    source = json.loads(BOOK.read_text(encoding="utf-8"))
    return study(BARS, BROAD21), build_depth_stress(source), source


def test_reproducible_offline_nonpromoting_terminal(originals):
    first = render(*originals, BARS)
    assert first == render(*originals, BARS)
    assert first.startswith("<!doctype html>")
    assert "NO ORDER AUTHORITY" in first
    assert "PROFITABILITY_NOT_PROVEN" in first
    assert '"promotion_allowed":false' in first
    assert '"account_mark_settlement_verified":false' in first
    assert '"visible_depth_is_historical":false' in first
    assert originals[0]["report_sha256"] in first
    assert originals[1]["report_sha256"] in first
    assert "__RESEARCH_PAYLOAD__" not in first


def test_all_210_and_144_research_rows_and_no_network_calls(originals):
    html = render(*originals, BARS)
    assert html.count('"return_pct":') == 210
    assert html.count('"hypothetical_fee_bps":') == 144
    assert "<script src=" not in html
    assert "fetch(" not in html
    assert "WebSocket(" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert 'id="arm"' in html
    assert 'id="haircut"' in html
    assert "width=device-width" in html
    assert "@media(max-width:550px)" in html


def test_changing_research_trade_cannot_impersonate_source(originals):
    coin, depth, source = originals
    corrupted = copy.deepcopy(coin)
    corrupted["scenarios"][0]["return_pct"] = "100000000"
    with pytest.raises(ExecutionConsoleError, match="differs"):
        render(corrupted, depth, source, BARS)


def test_changing_order_book_rejects_render(originals):
    coin, depth, source = originals
    altered = copy.deepcopy(source)
    symbol = "BTCUSDT"
    altered["observations"][symbol]["book"]["asks"][0]["qty"] = "9999999"
    with pytest.raises(ValueError):
        render(coin, depth, altered, BARS)


def test_permission_promotion_disallowed_by_verified_renderer(originals):
    coin, depth, source = originals
    altered = copy.deepcopy(depth)
    altered["live_trading_allowed"] = True
    with pytest.raises(ExecutionConsoleError, match="differs"):
        render(coin, altered, source, BARS)
