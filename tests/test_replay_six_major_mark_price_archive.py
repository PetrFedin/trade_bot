"""Six-major offline archive admission: isolated synthetic-source tests."""
import hashlib
import json
from datetime import timedelta

import pytest

from tests.test_replay_mark_price_hourly_archive import START, _case
from tools.replay_six_major_mark_price_archive import (
    SourceSetError,
    replay_six_major_source_set,
)
from tools.summarize_mark_price_hourly_sensitivity import SIX


def _six(tmp_path):
    from pathlib import Path
    import shutil

    captured = tmp_path / "captured"
    funding = tmp_path / "funding"
    captured.mkdir()
    funding.mkdir()
    source_case = tmp_path / "single"
    source_case.mkdir()
    original, rates, _ = _case(source_case)
    template = original / "BTCUSDT"
    funding_template = rates / "BTCUSDT_funding.csv"
    funding_meta = rates / "BTCUSDT_funding.manifest.json"
    pins = {}
    for symbol in SIX:
        folder = captured / symbol
        shutil.copytree(template, folder)
        for filename in list(folder.rglob("*")):
            if filename.is_file():
                raw = filename.read_text()
                raw = raw.replace("BTCUSDT", symbol)
                filename.write_text(raw)
        # Source hashes must match symbol-rewritten files and page payloads.
        mark_csv = folder / f"{symbol}_mark_60m.csv"
        join_csv = folder / f"{symbol}_funding_mark_60m.csv"
        for old, new in [
            (folder / "BTCUSDT_mark_60m.csv", mark_csv),
            (folder / "BTCUSDT_funding_mark_60m.csv", join_csv),
        ]:
            old.rename(new)
        page = folder / "provider_pages" / "BTCUSDT_mark_60m_page_000.json"
        page.rename(page.parent / f"{symbol}_mark_60m_page_000.json")
        rate_path = funding / f"{symbol}_funding.csv"
        rate_path.write_text(funding_template.read_text().replace("BTCUSDT", symbol))
        meta_path = funding / f"{symbol}_funding.manifest.json"
        rate_meta = json.loads(funding_meta.read_text())
        rate_meta["symbol"] = symbol
        rate_meta["dataset_sha256"] = hashlib.sha256(rate_path.read_bytes()).hexdigest()
        meta_path.write_text(json.dumps(rate_meta))
        old_manifest = folder / "BTCUSDT_mark_60m.manifest.json"
        mark = json.loads(old_manifest.read_text())
        mark["symbol"] = symbol
        mark["raw_mark_sha256"] = hashlib.sha256(mark_csv.read_bytes()).hexdigest()
        mark["settlement_mark_sha256"] = hashlib.sha256(join_csv.read_bytes()).hexdigest()
        mark["funding_source_sha256"] = hashlib.sha256(rate_path.read_bytes()).hexdigest()
        mark["funding_manifest_sha256"] = hashlib.sha256(meta_path.read_bytes()).hexdigest()
        query = mark["provider_query_pages"][0]
        payload_file = page.parent / f"{symbol}_mark_60m_page_000.json"
        payload = json.loads(payload_file.read_text())
        response = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        payload_file.write_bytes(response)
        query["provider_response_path"] = f"provider_pages/{payload_file.name}"
        query["provider_response_sha256"] = hashlib.sha256(response).hexdigest()
        result = json.dumps(
            payload["result"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        query["provider_result_sha256"] = hashlib.sha256(result).hexdigest()
        query["query"]["symbol"] = symbol
        manifest_path = folder / f"{symbol}_mark_60m.manifest.json"
        old_manifest.rename(manifest_path)
        manifest_path.write_text(json.dumps(mark, indent=2, sort_keys=True) + "\n")
        pins[symbol] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
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
