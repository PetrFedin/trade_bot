"""Six-major offline archive admission: isolated synthetic-source tests."""
import hashlib
import json
from datetime import timedelta

import pytest

import tools.replay_six_major_mark_price_archive as cohort
from tests.test_replay_mark_price_hourly_archive import START, _case
from tools.replay_six_major_mark_price_archive import (
    SourceSetError,
    replay_six_major_source_set,
)
from tools.summarize_mark_price_hourly_sensitivity import SIX


def _six(tmp_path):
    from tools.mark_price_hourly_sensitivity import collect_symbol

    captured = tmp_path / "captured"
    funding = tmp_path / "funding"
    captured.mkdir()
    funding.mkdir()
    original, rates, _ = _case(tmp_path / "single")
    source_response = json.loads(
        (original / "BTCUSDT" / "provider_pages" /
         "BTCUSDT_mark_60m_page_000.json").read_text()
    )
    original_funding = rates / "BTCUSDT_funding.csv"
    original_meta = rates / "BTCUSDT_funding.manifest.json"
    pins = {}
    for symbol in SIX:
        funding_csv = funding / f"{symbol}_funding.csv"
        funding_csv.write_text(
            original_funding.read_text().replace("BTCUSDT", symbol)
        )
        meta = json.loads(original_meta.read_text())
        meta["symbol"] = symbol
        meta["dataset_sha256"] = hashlib.sha256(
            funding_csv.read_bytes()
        ).hexdigest()
        funding_manifest = funding / f"{symbol}_funding.manifest.json"
        funding_manifest.write_text(json.dumps(meta))
        payload = json.loads(json.dumps(source_response))
        payload["result"]["symbol"] = symbol

        class CapturedResponse:
            status_code = 200

            def __init__(self, value):
                self.value = value

            def raise_for_status(self):
                pass

            def json(self):
                return self.value

        def recorded_only(url, *, params, timeout, fixed_symbol=symbol, data=payload):
            assert params["symbol"] == fixed_symbol
            return CapturedResponse(data)

        collect_symbol(
            symbol=symbol, start=START,
            end=START + timedelta(hours=23),
            funding_csv=funding_csv, funding_manifest=funding_manifest,
            output_dir=captured / symbol, get=recorded_only,
            pause_seconds=0,
        )
        manifest_path = captured / symbol / f"{symbol}_mark_60m.manifest.json"
        pins[symbol] = hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()
    spec = {
        "schema_version": "astra-six-major-hourly-mark-source-set-v1",
        "symbols": list(SIX),
        "mark_manifest_sha256": pins,
        "start": START.isoformat(),
        "end": (START + timedelta(hours=23)).isoformat(),
        "research_only": True,
        "origin_claim": "UNSIGNED_PUBLIC_PROVIDER_RESPONSE",
    }
    for field in (
        "provider_origin_verified", "account_funding_ledger_verified",
        "exact_settlement_mark_verified", "strategy_replay_completed",
        "strategy_promotion_allowed", "live_trading_allowed",
        "profitability_proven",
    ):
        spec[field] = False
    spec_path = tmp_path / "approved.json"
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    pin = hashlib.sha256(spec_path.read_bytes()).hexdigest()
    return spec_path, pin, captured, funding


def _run(case, target):
    spec, pin, captured, funding = case
    return replay_six_major_source_set(
        source_set_path=spec, expected_source_set_sha256=pin,
        archived_root=captured, funding_root=funding, output_dir=target,
    )


def test_six_symbol_atomic_publication(tmp_path):
    case = _six(tmp_path)
    destination = tmp_path / "qualified"
    report = _run(case, destination)
    assert report["symbols"] == list(SIX)
    assert report["hourly_rows_total"] == 6 * 24
    assert report["funding_settlements_total"] == 6 * 3
    assert report["strategy_promotion_allowed"] is False
    assert (destination / "source-set-receipt.json").is_file()
    assert (destination / "summary.json").is_file()


def test_missing_sixth_symbol_leaves_no_output(tmp_path):
    case = _six(tmp_path)
    spec, pin, root, _ = case
    (root / SIX[-1] / f"{SIX[-1]}_mark_60m.manifest.json").unlink()
    destination = tmp_path / "qualified"
    with pytest.raises(SourceSetError, match="preflight"):
        _run(case, destination)
    assert not destination.exists()


def test_bad_detached_source_set_pin_fails_before_output(tmp_path):
    spec, pin, root, funding = _six(tmp_path)
    destination = tmp_path / "qualified"
    with pytest.raises(SourceSetError, match="approval invalid"):
        replay_six_major_source_set(
            source_set_path=spec, expected_source_set_sha256="f" * 64,
            archived_root=root, funding_root=funding, output_dir=destination,
        )
    assert not destination.exists()


def test_output_within_source_tree_fails_closed(tmp_path):
    case = _six(tmp_path)
    with pytest.raises(SourceSetError, match="overlap"):
        _run(case, case[2] / "danger")


def test_existing_target_not_overwritten(tmp_path):
    case = _six(tmp_path)
    destination = tmp_path / "qualified"
    destination.mkdir()
    (destination / "keep.txt").write_text("safe")
    with pytest.raises(SourceSetError, match="already exists"):
        _run(case, destination)
    assert (destination / "keep.txt").read_text() == "safe"

def test_failure_after_three_imports_keeps_destination_absent(tmp_path, monkeypatch):
    case = _six(tmp_path)
    target = tmp_path / "qualified"
    original = cohort.replay_archived_symbol
    calls = []

    def interrupted_replay(**kwargs):
        calls.append(kwargs["symbol"])
        if len(calls) == 4:
            raise cohort.ArchiveReplayError("simulated fourth-symbol failure")
        return original(**kwargs)

    monkeypatch.setattr(cohort, "replay_archived_symbol", interrupted_replay)
    with pytest.raises(SourceSetError, match="no source set published"):
        _run(case, target)
    assert len(calls) == 4
    assert not target.exists()
    assert not list(tmp_path.glob(".astra-six-major-staging-*"))


def test_source_set_claiming_live_authority_fails_closed(tmp_path):
    spec, pin, root, funding = _six(tmp_path)
    claim = json.loads(spec.read_text())
    claim["live_trading_allowed"] = True
    spec.write_text(json.dumps(claim, indent=2, sort_keys=True) + "\n")
    revised_pin = hashlib.sha256(spec.read_bytes()).hexdigest()
    target = tmp_path / "qualified"
    with pytest.raises(SourceSetError, match="unsafe source-set claim"):
        replay_six_major_source_set(
            source_set_path=spec, expected_source_set_sha256=revised_pin,
            archived_root=root, funding_root=funding, output_dir=target,
        )
    assert not target.exists()
