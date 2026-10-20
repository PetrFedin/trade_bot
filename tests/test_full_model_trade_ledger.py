"""Full model trade ledger: synthetic tests only, no exchange fills."""
from __future__ import annotations

import csv
import errno
import hashlib
import io
import json
from pathlib import Path

import pytest

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tests.test_historical_execution_journal import report, synthetic_auditor_receipt
from tools.full_model_trade_ledger import (
    DECISION_FIELDS,
    POSITION_FIELDS,
    TRADE_FIELDS,
    TapeError,
    build_export,
    publish_bundle,
    safe_symbol,
)
from tools.historical_execution_journal import build_journal


def setup() -> tuple[dict, dict, dict]:
    raw = report()
    verifier = synthetic_auditor_receipt(raw)
    journal = build_journal(raw, verifier)
    return raw, verifier, journal


def fixture_export() -> tuple[dict, dict[str, bytes]]:
    raw, verifier, journal = setup()
    return build_export(raw, verifier, journal, journal["journal_sha256"])


def rows(payload: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))


def test_full_36_model_csv_population_and_provenance():
    manifest, files = fixture_export()
    assert manifest["schema_version"] == "astra-complete-modelled-trade-ledger-v1"
    assert manifest["model_count"] == 36
    assert manifest["scenario_count"] == 6
    assert manifest["totals"]["closed_trades"] == 36
    assert manifest["totals"]["terminal_open_positions"] == 0
    assert manifest["totals"]["daily_model_decisions"] == 36 * 95
    assert manifest["totals"]["entered_positions"] == 36
    assert manifest["totals"]["selected_without_entry_observations"] == 36 * 94
    assert manifest["by_exit_reason"] if "by_exit_reason" in manifest else True
    assert len(rows(files["closed-trades.csv"])) == 36
    assert len(rows(files["final-open-positions.csv"])) == 0
    assert len(rows(files["daily-decisions.csv"])) == 36 * 95
    for name, fields in (
        ("closed-trades.csv", TRADE_FIELDS),
        ("final-open-positions.csv", POSITION_FIELDS),
        ("daily-decisions.csv", DECISION_FIELDS),
    ):
        assert files[name].split(b"\n", 1)[0] == ",".join(fields).encode()
        assert manifest["csv_files"][name]["sha256"] == hashlib.sha256(
            files[name]
        ).hexdigest()
    source = rows(files["closed-trades.csv"])[0]
    assert source["net_pnl_excluding_funding_usdt"] == "10"
    assert source["execution_evidence_class"] == "MODELLED_DAILY_BAR"
    assert source["exit_bar_day_utc"] == "2024-04-04"
    assert manifest["daily_bar_timestamps_are_not_execution_timestamps"] is True
    assert manifest["actual_exchange_fills_verified"] is False
    assert manifest["live_trading_allowed"] is False


def test_deterministic_complete_export_same_inputs_same_bytes():
    first, first_files = fixture_export()
    second, second_files = fixture_export()
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first_files == second_files


def test_can_reconcile_36_model_csv_counts_with_auditor():
    raw, verifier, journal = setup()
    result, files = build_export(raw, verifier, journal, journal["journal_sha256"])
    assert len(rows(files["closed-trades.csv"])) == verifier["closed_trades_checked"]
    assert len(rows(files["daily-decisions.csv"])) == verifier["daily_equity_rows_checked"]
    assert result["totals"]["closed_trades"] == verifier["closed_trades_checked"]


@pytest.mark.parametrize("change", [
    "wrong_detached_sha", "source_trade_edit", "fake_approved_journal",
    "missing_auditor_event", "missing_trade", "unsafe_live_claim",
    "missing_model", "duplicate_model_scenario",
])
def test_corrupt_research_package_cannot_export(change):
    raw, verifier, journal = setup()
    approval = journal["journal_sha256"]
    if change == "wrong_detached_sha":
        approval = "0" * 64
    elif change == "source_trade_edit":
        raw["comparisons"][0]["models"]["DAILY_STOP_FIRST:NO_FUNDING"][
            "trades"
        ][0]["net_pnl"] = "999"
    elif change == "fake_approved_journal":
        journal["comparisons"][0]["models"]["DAILY_STOP_FIRST:NO_FUNDING"][
            "portfolio_return_pct"
        ] = "500"
        journal["journal_sha256"] = hashlib.sha256(canonical_json_bytes({
            k: v for k, v in journal.items() if k != "journal_sha256"
        })).hexdigest()
    elif change == "missing_auditor_event":
        verifier["closed_trades_checked"] = 35
    elif change == "missing_trade":
        raw["comparisons"][0]["models"]["DAILY_STOP_FIRST:NO_FUNDING"][
            "trades"
        ].clear()
    elif change == "unsafe_live_claim":
        journal["live_trading_allowed"] = True
    elif change == "missing_model":
        raw["comparisons"][0]["models"].pop("DAILY_STOP_FIRST:NO_FUNDING")
    else:
        raw["comparisons"][1] = raw["comparisons"][0]
    with pytest.raises(TapeError):
        build_export(raw, verifier, journal, approval)


@pytest.mark.parametrize("untrusted", [
    "=1+1", "+SUM(A1)", "-4+5", "@cmd", "BTCUSDT\n=HYPERLINK",
    "btcUSDT", "<script>", "BTC-USDT",
])
def test_unsafe_symbol_including_csv_formula_and_html_refused(untrusted):
    with pytest.raises(TapeError, match="unsafe"):
        safe_symbol(untrusted, [untrusted])


def test_immutable_publication_full_csv_and_manifest(tmp_path):
    manifest, payloads = fixture_export()
    dest = tmp_path / "export"
    publish_bundle(dest, manifest, payloads)
    for name, payload in payloads.items():
        assert (dest / name).read_bytes() == payload
    actual = json.loads((dest / "complete-manifest.json").read_text())
    assert actual["manifest_sha256"] == manifest["manifest_sha256"]
    assert actual["csv_files"] == manifest["csv_files"]
    assert actual["totals"] == manifest["totals"]
    with pytest.raises(TapeError, match="already exists"):
        publish_bundle(dest, manifest, payloads)
    assert actual == json.loads((dest / "complete-manifest.json").read_text())


def test_manifest_never_written_after_second_csv_fails(tmp_path, monkeypatch):
    import tools.full_model_trade_ledger as publisher

    manifest, payloads = fixture_export()
    original_link = publisher.os.link
    attempts = 0

    def interrupted(src, dst, *, follow_symlinks):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError(errno.EOPNOTSUPP, "hard links unavailable")
        return original_link(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(publisher.os, "link", interrupted)
    root = tmp_path / "export"
    with pytest.raises(TapeError, match="atomic CSV"):
        publish_bundle(root, manifest, payloads)
    assert (root / "closed-trades.csv").exists()
    assert not (root / "complete-manifest.json").exists()
    assert not list(root.glob(".astra-full-tape-*"))


def test_symlink_output_parent_is_rejected(tmp_path):
    dest = tmp_path / "real"
    dest.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(dest, target_is_directory=True)
    manifest, payloads = fixture_export()
    with pytest.raises(TapeError, match="symlinked"):
        publish_bundle(alias, manifest, payloads)
    assert not list(dest.iterdir())


def test_manifest_last_after_csv_population(tmp_path, monkeypatch):
    import tools.full_model_trade_ledger as publisher

    original = publisher.publish_new_json_receipt
    files_seen = []

    def on_manifest(dest: Path, receipt: dict) -> None:
        files_seen.extend(sorted(p.name for p in dest.parent.glob("*.csv")))
        assert set(files_seen) == {
            "closed-trades.csv", "daily-decisions.csv", "final-open-positions.csv"
        }
        original(dest, receipt)

    monkeypatch.setattr(publisher, "publish_new_json_receipt", on_manifest)
    manifest, payloads = fixture_export()
    publish_bundle(tmp_path / "out", manifest, payloads)
    assert len(files_seen) == 3
