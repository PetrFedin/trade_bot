"""Tamper, accessibility and no-network checks for research-only visual audit."""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from tools.independent_coin_strategy_lab import canonical
from tools.risk_gate_terminal import (
    TEMPLATE,
    RiskGateUIError,
    render,
    verify_digest,
)


def stamped(value: dict) -> dict:
    import hashlib

    result = copy.deepcopy(value)
    result["report_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def test_reject_tampered_source_document():
    source = stamped({
        "schema_version": "unknown", "profitability_proven": False,
        "strategy_promotion_allowed": False, "live_trading_allowed": False,
    })
    verify_digest(source)
    source["live_trading_allowed"] = True
    with pytest.raises(RiskGateUIError, match="hash"):
        verify_digest(source)


def test_reject_schema_impersonation_after_digest_change():
    doc = stamped({
        "schema_version": "synthetic", "profitability_proven": False,
        "strategy_promotion_allowed": False, "live_trading_allowed": False,
    })
    with pytest.raises(RiskGateUIError, match="source join"):
        render(doc, doc, doc)


def test_readonly_document_has_unique_native_selects_and_no_external_assets():
    page = Path(TEMPLATE).read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([^"]+)"', page)
    assert len(ids) == len(set(ids))
    for control in ("universe", "strategy", "priority", "cost", "gate"):
        assert control in ids
        assert f'for="{control}"' in page
    assert "<!doctype html>" in page
    assert "width=device-width" in page
    assert "@media(max-width:590px)" in page
    assert "NO EXECUTION AUTHORITY" in page
    assert "PROFITABILITY_NOT_PROVEN" in page
    assert 'href="execution-quality-terminal.html"' in page
    assert 'href="operator-status.html"' in page
    assert "<script src=" not in page
    assert "fetch(" not in page
    assert "WebSocket(" not in page
    assert "http://" not in page
    assert "https://" not in page
