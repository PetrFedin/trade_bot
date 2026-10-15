"""Tests for coarse Bybit Mark Price source sensitivity collector."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from tools.mark_price_hourly_sensitivity import (
    HOUR,
    HOUR_MS,
    LIMIT,
    URL,
    HourlyMarkSourceError,
    collect_symbol,
    fetch_hourly_marks,
)

D = Decimal
START = datetime(2026, 1, 1, tzinfo=UTC)


class FakeResponse:
    def __init__(self, payload, *, status_error: bool = False):
        self.payload = payload
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise requests.HTTPError("synthetic HTTP failure")

    def json(self):
        return self.payload


def _row(stamp: datetime, price: int):
    return [
        str(int(stamp.timestamp() * 1000)),
        str(price),
        str(price + 1),
        str(price - 1),
        str(price) + ".5",
    ]


def _fake_get(*, omit: datetime | None = None):
    calls = []

    def get(url, *, params, timeout):
        assert url == URL
        assert timeout == 30
        calls.append(dict(params))
        start = datetime.fromtimestamp(params["start"] / 1000, tz=UTC)
        end = datetime.fromtimestamp(params["end"] / 1000, tz=UTC)
        rows = []
        stamp = start
        while stamp <= end:
            if stamp != omit:
                price = 100 + int((stamp - START) / HOUR)
                rows.append(_row(stamp, price))
            stamp += HOUR
        rows.reverse()
        return FakeResponse({
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "linear",
                "symbol": params["symbol"],
                "list": rows,
            },
        })

    return get, calls


def _funding_fixture(root: Path, *, symbol: str = "BTCUSDT"):
    funding_csv = root / f"{symbol}_funding.csv"
    rows = [
        (START, "0.0001"),
        (START + timedelta(hours=8), "-0.0002"),
        (START + timedelta(hours=16), "0.0003"),
    ]
    with funding_csv.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "symbol", "funding_rate"])
        for stamp, rate in rows:
            writer.writerow([stamp.isoformat(), symbol, rate])
    digest = hashlib.sha256(funding_csv.read_bytes()).hexdigest()
    manifest = root / f"{symbol}_funding.manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "bybit-funding-snapshot-v2",
        "symbol": symbol,
        "category": "linear",
        "dataset_sha256": digest,
        "settlement_count": len(rows),
    }), encoding="utf-8")
    return funding_csv, manifest


def test_fetches_reverse_provider_rows_into_complete_ascending_hours() -> None:
    get, calls = _fake_get()
    rows, pages = fetch_hourly_marks(
        symbol="BTCUSDT",
        start=START,
        end=START + timedelta(hours=5),
        get=get,
        pause_seconds=0,
    )
    assert len(rows) == 6
    assert [row.timestamp for row in rows] == [
        START + timedelta(hours=i) for i in range(6)
    ]
    assert len(pages) == len(calls) == 1
    assert len(pages[0]["provider_result_sha256"]) == 64


def test_paginates_without_overlap_or_gap_after_1000_hours() -> None:
    get, calls = _fake_get()
    end = START + timedelta(hours=LIMIT + 1)
    rows, pages = fetch_hourly_marks(
        symbol="BTCUSDT",
        start=START,
        end=end,
        get=get,
        pause_seconds=0,
    )
    assert len(rows) == LIMIT + 2
    assert len(pages) == len(calls) == 2
    assert calls[0]["end"] + HOUR_MS == calls[1]["start"]
    assert rows[0].timestamp == START
    assert rows[-1].timestamp == end


def test_missing_provider_hour_fails_closed() -> None:
    missing = START + timedelta(hours=2)
    get, _ = _fake_get(omit=missing)
    with pytest.raises(HourlyMarkSourceError, match="continuity mismatch"):
        fetch_hourly_marks(
            symbol="BTCUSDT",
            start=START,
            end=START + timedelta(hours=4),
            get=get,
            pause_seconds=0,
        )


def test_source_http_failure_retries_then_fails() -> None:
    calls = 0

    def broken(url, *, params, timeout):
        nonlocal calls
        calls += 1
        return FakeResponse({}, status_error=True)

    with pytest.raises(HourlyMarkSourceError, match="after retries"):
        fetch_hourly_marks(
            symbol="BTCUSDT",
            start=START,
            end=START,
            get=broken,
            pause_seconds=0,
        )
    assert calls == 5


def test_collect_symbol_intersects_only_real_funding_hours(tmp_path: Path) -> None:
    funding_csv, funding_manifest = _funding_fixture(tmp_path)
    get, _ = _fake_get()
    out = tmp_path / "out"
    report = collect_symbol(
        symbol="BTCUSDT",
        start=START,
        end=START + timedelta(hours=23),
        funding_csv=funding_csv,
        funding_manifest=funding_manifest,
        output_dir=out,
        get=get,
        pause_seconds=0,
    )
    assert report["hourly_rows"] == 24
    assert report["funding_settlements"] == 3
    assert report["interval_minutes"] == 60
    assert report["exact_settlement_mark_verified"] is False
    assert report["compatible_with_final_1m_admission"] is False
    assert report["strategy_promotion_allowed"] is False
    assert len(report["raw_mark_sha256"]) == 64
    assert len(report["settlement_mark_sha256"]) == 64
    settlement = list(csv.DictReader(
        (out / "BTCUSDT_funding_mark_60m.csv").open(encoding="utf-8")
    ))
    assert [row["timestamp"] for row in settlement] == [
        START.isoformat(),
        (START + timedelta(hours=8)).isoformat(),
        (START + timedelta(hours=16)).isoformat(),
    ]


def test_funding_manifest_tamper_fails_before_collection_output(
    tmp_path: Path,
) -> None:
    funding_csv, funding_manifest = _funding_fixture(tmp_path)
    funding_csv.write_text(
        funding_csv.read_text(encoding="utf-8").replace("0.0001", "0.9999"),
        encoding="utf-8",
    )
    get, _ = _fake_get()
    with pytest.raises(HourlyMarkSourceError, match="manifest mismatch"):
        collect_symbol(
            symbol="BTCUSDT",
            start=START,
            end=START + timedelta(hours=23),
            funding_csv=funding_csv,
            funding_manifest=funding_manifest,
            output_dir=tmp_path / "out",
            get=get,
            pause_seconds=0,
        )


def test_invalid_time_and_symbol_fail_closed() -> None:
    get, _ = _fake_get()
    with pytest.raises(HourlyMarkSourceError, match="align"):
        fetch_hourly_marks(
            symbol="BTCUSDT",
            start=START + timedelta(minutes=1),
            end=START + timedelta(hours=1),
            get=get,
            pause_seconds=0,
        )
    with pytest.raises(HourlyMarkSourceError, match="uppercase"):
        fetch_hourly_marks(
            symbol="btcusdt",
            start=START,
            end=START + timedelta(hours=1),
            get=get,
            pause_seconds=0,
        )
