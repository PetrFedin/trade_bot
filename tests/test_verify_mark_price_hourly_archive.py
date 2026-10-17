"""Synthetic-only rejection tests for the independent hourly Mark Price audit."""
import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from tools.verify_mark_price_hourly_archive import MarkArchiveError, verify_symbol_archive

START = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path):
    root = tmp_path / "marks"
    funding_root = tmp_path / "funding"
    folder = root / SYMBOL
    folder.mkdir(parents=True)
    funding_root.mkdir()
    def write(path, header, rows):
        with path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output)
            writer.writerow(header)
            writer.writerows(rows)

    timestamps = [(START + timedelta(hours=i)).isoformat() for i in range(24)]
    raw = folder / f"{SYMBOL}_mark_60m.csv"
    joined = folder / f"{SYMBOL}_funding_mark_60m.csv"
    funding = funding_root / f"{SYMBOL}_funding.csv"
    funding_manifest = funding_root / f"{SYMBOL}_funding.manifest.json"
    write(raw, ["timestamp", "symbol", "open", "high", "low", "close"],
          [[t, SYMBOL, "100", "101", "99", "100.5"] for t in timestamps])
    write(funding, ["timestamp", "symbol", "funding_rate"],
          [[timestamps[i], SYMBOL, "0.0001"] for i in (0, 8, 16)])
    funding_manifest.write_text(json.dumps({
        "schema_version": "bybit-funding-snapshot-v2", "symbol": SYMBOL,
        "category": "linear", "dataset_sha256": _sha(funding),
        "settlement_count": 3, "first_settlement": timestamps[0],
        "last_settlement": timestamps[16],
    }))
    write(joined, [
        "timestamp", "symbol", "funding_rate", "mark_open", "mark_high",
        "mark_low", "mark_close", "hour_range_fraction",
    ], [[timestamps[i], SYMBOL, "0.0001", "100", "101", "99", "100.5", "0.02"]
        for i in (0, 8, 16)])
    provider_result = {
        "category": "linear", "symbol": SYMBOL,
        "list": [
            [str(int((START + timedelta(hours=i)).timestamp() * 1000)),
             "100", "101", "99", "100.5"]
            for i in reversed(range(24))
        ],
    }
    payload = {"retCode": 0, "retMsg": "OK", "result": provider_result}
    def canonical(value):
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    provider_folder = folder / "provider_pages"
    provider_folder.mkdir()
    provider_file = provider_folder / f"{SYMBOL}_mark_60m_page_000.json"
    provider_file.write_bytes(canonical(payload))
    manifest = {
        "endpoint": "https://api.bytick.com/v5/market/mark-price-kline",
        "evaluation_class": "COARSE_PUBLIC_MARK_PRICE_ENVELOPE_RESEARCH_ONLY",
        "start": timestamps[0], "end": timestamps[-1],
        "hourly_rows": 24, "funding_settlements": 3,
        "raw_mark_sha256": _sha(raw), "settlement_mark_sha256": _sha(joined),
        "funding_source_sha256": _sha(funding),
        "funding_manifest_sha256": _sha(funding_manifest),
        "mean_settlement_hour_range_fraction": "0.02",
        "maximum_settlement_hour_range_fraction": "0.02",
        "provider_response_representation": "CANONICAL_DECODED_JSON_NOT_HTTP_WIRE",
        "provider_query_pages": [{
            "page": 0, "rows": 24, "first_hour": timestamps[0],
            "last_hour": timestamps[-1],
            "provider_result_sha256": hashlib.sha256(canonical(provider_result)).hexdigest(),
            "provider_response_path": f"provider_pages/{provider_file.name}",
            "provider_response_sha256": _sha(provider_file),
            "query": {
                "category": "linear", "symbol": SYMBOL, "interval": "60",
                "start": int(START.timestamp() * 1000),
                "end": int((START + timedelta(hours=23)).timestamp() * 1000),
                "limit": 1000,
            },
        }],
    }
    for flag in (
        "provider_payload_signature_verified", "exact_settlement_mark_verified",
        "account_funding_ledger_verified", "compatible_with_final_1m_admission",
        "strategy_replay_completed", "strategy_promotion_allowed", "profitability_proven",
    ):
        manifest[flag] = False
    return root, funding_root, manifest


def _verify(root, funding_root, manifest):
    return verify_symbol_archive(
        root=root, funding_root=funding_root, symbol=SYMBOL, mark_manifest=manifest
    )


def test_consistent_synthetic_archive(tmp_path):
    assert _verify(*_fixture(tmp_path))["funding_settlements"] == 3


def test_rehashed_candle_tamper_is_not_admitted(tmp_path):
    root, funding_root, manifest = _fixture(tmp_path)
    raw = root / SYMBOL / f"{SYMBOL}_mark_60m.csv"
    raw.write_text(raw.read_text().replace("100,101,99,100.5", "100,105,99,100.5", 1))
    manifest["raw_mark_sha256"] = _sha(raw)
    with pytest.raises(MarkArchiveError, match="settlement Mark Price differs"):
        _verify(root, funding_root, manifest)


def test_rehashed_funding_intersection_tamper_is_not_admitted(tmp_path):
    root, funding_root, manifest = _fixture(tmp_path)
    joined = root / SYMBOL / f"{SYMBOL}_funding_mark_60m.csv"
    joined.write_text(joined.read_text().replace("0.0001", "0.9", 1))
    manifest["settlement_mark_sha256"] = _sha(joined)
    with pytest.raises(MarkArchiveError, match="funding rate differs"):
        _verify(root, funding_root, manifest)


def test_missing_hour_with_fresh_checksum_is_not_admitted(tmp_path):
    root, funding_root, manifest = _fixture(tmp_path)
    raw = root / SYMBOL / f"{SYMBOL}_mark_60m.csv"
    lines = raw.read_text().splitlines()
    raw.write_text("\n".join(lines[:2] + lines[3:]) + "\n")
    manifest["raw_mark_sha256"] = _sha(raw)
    with pytest.raises(MarkArchiveError, match="hours are missing"):
        _verify(root, funding_root, manifest)


def test_provider_page_query_falsification_is_not_admitted(tmp_path):
    root, funding_root, manifest = _fixture(tmp_path)
    manifest["provider_query_pages"][0]["query"]["interval"] = "1"
    with pytest.raises(MarkArchiveError, match="query evidence mismatch"):
        _verify(root, funding_root, manifest)


def test_false_venue_account_claim_is_not_admitted(tmp_path):
    root, funding_root, manifest = _fixture(tmp_path)
    manifest["account_funding_ledger_verified"] = True
    with pytest.raises(MarkArchiveError, match="unsupported proof flag"):
        _verify(root, funding_root, manifest)


def test_rehashed_provider_response_tamper_rejected(tmp_path):
    root, funding_root, manifest = _fixture(tmp_path)
    source = root / SYMBOL / "provider_pages" / f"{SYMBOL}_mark_60m_page_000.json"
    payload = json.loads(source.read_bytes())
    payload["result"]["list"][0][2] = "120"
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    source.write_bytes(canonical)
    page = manifest["provider_query_pages"][0]
    page["provider_response_sha256"] = _sha(source)
    page["provider_result_sha256"] = hashlib.sha256(
        json.dumps(payload["result"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()
    with pytest.raises(MarkArchiveError, match="archived provider candle differs"):
        _verify(root, funding_root, manifest)


def test_missing_provider_response_fails_closed(tmp_path):
    root, funding_root, manifest = _fixture(tmp_path)
    (root / SYMBOL / "provider_pages" / f"{SYMBOL}_mark_60m_page_000.json").unlink()
    with pytest.raises(MarkArchiveError, match="provider response archive hash mismatch"):
        _verify(root, funding_root, manifest)
