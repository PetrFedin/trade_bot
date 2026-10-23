"""Source-joined, read-only shared-cash research terminal acceptance."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.depth_execution_research import build_depth_stress
from tools.execution_quality_console import ExecutionConsoleError, render
from tools.independent_coin_strategy_lab import BROAD21, study
from tools.shared_capital_candidate_study import study_shared

ARCHIVE = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
SOURCE = Path("research/snapshots/bybit_public_depth_observation_20261023.json")


@pytest.fixture(scope="module")
def genuine():
    original_book = json.loads(SOURCE.read_text(encoding="utf-8"))
    return (
        study(ARCHIVE, BROAD21), build_depth_stress(original_book),
        original_book, study_shared(ARCHIVE),
    )


def test_complete_source_join_and_responsive_shared_equity_chart(genuine):
    coin, depth, original_book, shared = genuine
    document = render(coin, depth, original_book, ARCHIVE, shared=shared)
    assert document.startswith("<!doctype html>")
    assert '"shared_portfolio_funding_verified":false' in document
    assert '"visible_depth_is_historical":false' in document
    assert '"promotion_allowed":false' in document
    assert 'id="portfolio-chart"' in document
    assert 'id="shared-comparison"' in document
    assert 'id="portfolio-priority"' in document
    assert document.count('"net_return_pct":') == 36
    assert document.count('"return_pct":') == 210
    assert document.count('"hypothetical_fee_bps":') == 144
    assert shared["report_sha256"] in document
    assert "__SHARED_SHA__" not in document
    assert 'href="operator-status.html"' in document
    assert "@media(max-width:580px)" in document
    assert "NO ORDER AUTHORITY" in document
    assert "PROFITABILITY_NOT_PROVEN" in document


def test_source_join_rejects_mutated_shared_trade_reconstruction(genuine):
    coin, depth, source, shared = genuine
    corrupt = copy.deepcopy(shared)
    corrupt["scenarios"][0]["ending_equity_usdt"] = "900000"
    with pytest.raises(ExecutionConsoleError, match="shared cash report"):
        render(coin, depth, source, ARCHIVE, shared=corrupt)


def test_model_cannot_promote_or_use_public_book_as_old_trades(genuine):
    coin, depth, source, shared = genuine
    corrupt = copy.deepcopy(shared)
    corrupt["current_orderbook_mixed_into_history"] = True
    with pytest.raises(ExecutionConsoleError, match="shared cash report"):
        render(coin, depth, source, ARCHIVE, shared=corrupt)


def test_source_history_cannot_be_mixed_with_alternate_daily_archive(genuine):
    coin, depth, source, shared = genuine
    tampered = copy.deepcopy(coin)
    tampered["source_sha256"]["BTCUSDT_D.csv"] = "0" * 64
    with pytest.raises(ExecutionConsoleError, match="candidate output differs"):
        render(tampered, depth, source, ARCHIVE, shared=shared)
