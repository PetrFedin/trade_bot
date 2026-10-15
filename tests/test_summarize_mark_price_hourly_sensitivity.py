"""Aggregate hourly Mark Price source evidence without promoting it to 1m proof."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from tools.summarize_mark_price_hourly_sensitivity import (
    SIX,
    HourlyMarkSummaryError,
    summarize,
)


def _write_symbol(root: Path, symbol: str, *, rows: int = 24, settlements: int = 3):
    directory = root / symbol
    directory.mkdir(parents=True)
    raw = directory / f"{symbol}_mark_60m.csv"
    settlement = directory / f"{symbol}_funding_mark_60m.csv"
    with raw.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "symbol", "open", "high", "low", "close"])
        for i in range(rows):
            writer.writerow([
                f"2026-01-01T{i:02d}:00:00+00:00",
                symbol, "100", "101", "99", "100.5",
            ])
    with settlement.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "timestamp", "symbol", "funding_rate",
            "mark_open", "mark_high", "mark_low", "mark_close",
            "hour_range_fraction",
        ])
        for i in range(settlements):
            writer.writerow([
                f"2026-01-01T{i * 8:02d}:00:00+00:00",
                symbol, "0.0001", "100", "101", "99", "100.5", "0.02",
            ])
    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    manifest = {
        "schema_version": "bybit-mark-price-hourly-sensitivity-v1",
        "evaluation_class": "COARSE_PUBLIC_MARK_PRICE_ENVELOPE_RESEARCH_ONLY",
        "symbol": symbol,
        "category": "linear",
        "endpoint": "https://api.bybit.com/v5/market/mark-price-kline",
        "interval_minutes": 60,
        "start": "2026-01-01T00:00:00+00:00",
        "end": "2026-01-01T23:00:00+00:00",
        "hourly_rows": rows,
        "funding_settlements": settlements,
        "raw_mark_sha256": sha(raw),
        "settlement_mark_sha256": sha(settlement),
        "mean_settlement_hour_range_fraction": "0.02",
        "maximum_settlement_hour_range_fraction": "0.02",
        "provider_query_pages": [{
            "page": 0,
            "provider_result_sha256": "a" * 64,
            "rows": rows,
        }],
        "exact_settlement_mark_verified": False,
        "compatible_with_final_1m_admission": False,
        "account_funding_ledger_verified": False,
        "strategy_replay_completed": False,
        "strategy_promotion_allowed": False,
        "profitability_proven": False,
    }
    (directory / f"{symbol}_mark_60m.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _fixture(root: Path):
    for symbol in SIX:
        _write_symbol(root, symbol)


def test_six_symbol_summary_keeps_coarse_status(tmp_path: Path) -> None:
    _fixture(tmp_path)
    result = summarize(tmp_path)
    assert result["symbols"] == list(SIX)
    assert result["hourly_rows_total"] == 24 * 6
    assert result["funding_settlements_total"] == 3 * 6
    assert result["provider_pages_total"] == 6
    assert result["weighted_mean_settlement_hour_range_fraction"] == "0.02"
    assert result["maximum_settlement_hour_range_fraction"] == "0.02"
    assert result["exact_settlement_mark_verified"] is False
    assert result["compatible_with_final_1m_admission"] is False
    assert result["strategy_replay_completed"] is False
    assert result["profitability_proven"] is False
    assert len(result["evidence_sha256"]) == 64


def test_missing_symbol_fails_closed(tmp_path: Path) -> None:
    _fixture(tmp_path)
    target = tmp_path / SIX[-1] / f"{SIX[-1]}_mark_60m.manifest.json"
    target.unlink()
    with pytest.raises(HourlyMarkSummaryError, match="manifest missing"):
        summarize(tmp_path)


def test_tampered_raw_source_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path)
    raw = tmp_path / SIX[0] / f"{SIX[0]}_mark_60m.csv"
    raw.write_text(raw.read_text().replace("100,101", "100,102", 1))
    with pytest.raises(HourlyMarkSummaryError, match="hash mismatch"):
        summarize(tmp_path)


def test_mixed_collection_window_rejected(tmp_path: Path) -> None:
    _fixture(tmp_path)
    path = tmp_path / SIX[0] / f"{SIX[0]}_mark_60m.manifest.json"
    data = json.loads(path.read_text())
    data["end"] = "2026-01-02T00:00:00+00:00"
    path.write_text(json.dumps(data))
    with pytest.raises(HourlyMarkSummaryError, match="chronology differs"):
        summarize(tmp_path)


def test_final_1m_claim_cannot_enter_coarse_summary(tmp_path: Path) -> None:
    _fixture(tmp_path)
    path = tmp_path / SIX[0] / f"{SIX[0]}_mark_60m.manifest.json"
    data = json.loads(path.read_text())
    data["compatible_with_final_1m_admission"] = True
    path.write_text(json.dumps(data))
    with pytest.raises(HourlyMarkSummaryError, match="contract mismatch"):
        summarize(tmp_path)
