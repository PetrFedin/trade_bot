"""Synthetic adversarial input and immutable publication tests for offline dashboard."""
from __future__ import annotations

import copy
import errno
import hashlib

import pytest

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tests.test_historical_execution_journal import report, synthetic_auditor_receipt
from tools.historical_execution_journal import build_journal
from tools.research_control_tower import (
    TowerError,
    publish,
    render,
    require_detached_journal_pin,
)


def mock_journal() -> dict:
    raw = report()
    return build_journal(raw, synthetic_auditor_receipt(raw))


def resign(row: dict) -> None:
    row["journal_sha256"] = hashlib.sha256(canonical_json_bytes({
        key: val for key, val in row.items() if key != "journal_sha256"
    })).hexdigest()


def test_all_models_and_no_automation_controls():
    page = render(mock_journal())
    assert page.startswith("<!doctype html>")
    assert "36/36" in page
    assert page.count("<details>") == 42
    assert "PROFITABILITY_NOT_PROVEN" in page
    assert "LIVE HOLD" in page
    assert "90 дней" in page
    assert "<script" not in page
    assert "<form" not in page
    assert "<button" not in page
    assert "https://" not in page


def test_same_input_same_html_bytes():
    j = mock_journal()
    assert render(j) == render(j)


@pytest.mark.parametrize("field", [
    "live_trading_allowed", "strategy_promotion_allowed",
    "account_funding_ledger_verified", "profitability_proven",
    "settlement_mark_price_verified", "actual_exchange_fills_verified",
    "untouched_holdout",
])
def test_rehashed_authority_claims_cannot_promote(field):
    j = mock_journal()
    j[field] = True
    resign(j)
    with pytest.raises(TowerError, match="unsafe"):
        render(j)


def test_modified_journal_without_digest_fails():
    j = mock_journal()
    j["model_count"] = 35
    with pytest.raises(TowerError, match="modified"):
        render(j)


@pytest.mark.parametrize("case", [
    "rehashed_model_count", "missing_model", "missing_group",
    "altered_net", "missing_trade", "altered_window",
    "wrong_matched_return", "unsafe_partial_window", "fake_funding",
])
def test_rehashed_false_financial_evidence_rejected(case):
    j = mock_journal()
    model = j["comparisons"][0]["models"]["DAILY_STOP_FIRST:NO_FUNDING"]
    if case == "rehashed_model_count":
        j["model_count"] = 35
    elif case == "missing_model":
        j["comparisons"][0]["models"].pop("DAILY_STOP_FIRST:NO_FUNDING")
    elif case == "missing_group":
        j["comparisons"].pop()
    elif case == "altered_net":
        model["by_symbol"]["BTCUSDT"]["closed_trade_net_usd"] = "999"
    elif case == "missing_trade":
        model["windows"][1]["closed_trade_count"] = 0
    elif case == "altered_window":
        model["windows"][0]["ending_equity_usd"] = "9000"
    elif case == "wrong_matched_return":
        j["matched_universe_differences"][0]["six_return_pct"] = "99"
    elif case == "unsafe_partial_window":
        model["windows"][1]["complete_window"] = True
    else:
        model["funding_cash_net_usd"] = "5"
    resign(j)
    with pytest.raises(TowerError):
        render(j)


def test_html_escapes_untrusted_instrument_name():
    j = mock_journal()
    m = j["comparisons"][0]["models"]["DAILY_STOP_FIRST:NO_FUNDING"]
    payload = '<img src=x onerror="alert(1)">'
    item = m["by_symbol"].pop("BTCUSDT")
    m["by_symbol"][payload] = item
    resign(j)
    page = render(j)
    assert payload not in page
    assert "&lt;img" in page
    assert "<img" not in page


def test_atomic_publish_creates_complete_html_once(tmp_path):
    target = tmp_path / "view.html"
    page = render(mock_journal())
    publish(target, page)
    assert target.read_text() == page
    with pytest.raises(TowerError, match="already exists"):
        publish(target, "overwritten")
    assert target.read_text() == page
    assert not list(tmp_path.glob(".astra-tower-*"))


def test_competing_writer_cannot_be_overwritten(tmp_path, monkeypatch):
    import tools.research_control_tower as tower

    target = tmp_path / "view.html"

    def competitor(source, destination, *, follow_symlinks):
        assert follow_symlinks is False
        destination.write_text("created by competitor")
        raise FileExistsError(errno.EEXIST, "already exists")

    monkeypatch.setattr(tower.os, "link", competitor)
    with pytest.raises(TowerError, match="appeared"):
        publish(target, "new")
    assert target.read_text() == "created by competitor"
    assert not list(tmp_path.glob(".astra-tower-*"))


def test_no_replace_of_symlink_target(tmp_path):
    source = tmp_path / "protected.html"
    source.write_text("do not touch")
    target = tmp_path / "view.html"
    target.symlink_to(source)
    with pytest.raises(TowerError, match="already exists"):
        publish(target, "new")
    assert source.read_text() == "do not touch"


def test_symlink_parent_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(TowerError, match="symlinked"):
        publish(alias / "view.html", "new")
    assert not list(real.iterdir())


def test_unsupported_hardlink_fails_closed(tmp_path, monkeypatch):
    import tools.research_control_tower as tower

    def refuse(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "not supported")

    monkeypatch.setattr(tower.os, "link", refuse)
    target = tmp_path / "view.html"
    with pytest.raises(TowerError, match="no safe"):
        publish(target, "new")
    assert not target.exists()
    assert not list(tmp_path.glob(".astra-tower-*"))


def test_detached_sha_rejects_rehashed_but_unapproved_replacement():
    original = mock_journal()
    approved = original["journal_sha256"]
    require_detached_journal_pin(original, approved)
    revised = copy.deepcopy(original)
    revised["comparisons"][0]["models"]["DAILY_STOP_FIRST:NO_FUNDING"][
        "model_key"
    ] = "HOURLY_COLLISION_REFINED:NO_FUNDING"
    resign(revised)
    with pytest.raises(TowerError, match="digest mismatch"):
        require_detached_journal_pin(revised, approved)


@pytest.mark.parametrize("pin", ["", "incorrect", "e" * 63, None])
def test_detached_pin_missing_or_malformed_fails_closed(pin):
    with pytest.raises(TowerError, match="detached journal pin"):
        require_detached_journal_pin(mock_journal(), pin)
