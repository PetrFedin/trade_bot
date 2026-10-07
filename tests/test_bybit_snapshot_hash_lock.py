from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from tools.fetch_bybit_funding import write_dataset as write_funding_dataset
from tools.fetch_bybit_klines import Request, write_dataset as write_kline_dataset


def test_bybit_kline_snapshot_manifest_hash_locks_csv(tmp_path: Path) -> None:
    bars = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "symbol": "BTCUSDT",
            "open": "100",
            "high": "110",
            "low": "95",
            "close": "105",
            "volume": "1000",
        },
        {
            "timestamp": "2026-01-02T00:00:00+00:00",
            "symbol": "BTCUSDT",
            "open": "105",
            "high": "112",
            "low": "101",
            "close": "108",
            "volume": "1200",
        },
    ]
    request = Request(
        symbol="BTCUSDT",
        interval="D",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 3, tzinfo=UTC),
    )

    path = write_kline_dataset(bars, request, tmp_path)
    manifest = json.loads(
        (tmp_path / "BTCUSDT_D.manifest.json").read_text()
    )

    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest["dataset_sha256"] == expected

    path.write_bytes(path.read_bytes() + b"tamper")
    assert hashlib.sha256(path.read_bytes()).hexdigest() != manifest["dataset_sha256"]


def test_bybit_funding_snapshot_manifest_hash_locks_csv(tmp_path: Path) -> None:
    rows = [
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "symbol": "BTCUSDT",
            "funding_rate": "0.0001",
        },
        {
            "timestamp": "2026-01-01T08:00:00+00:00",
            "symbol": "BTCUSDT",
            "funding_rate": "-0.00005",
        },
    ]

    path = write_funding_dataset(
        rows,
        "BTCUSDT",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 2, tzinfo=UTC),
        tmp_path,
    )
    manifest = json.loads(
        (tmp_path / "BTCUSDT_funding.manifest.json").read_text()
    )

    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert manifest["dataset_sha256"] == expected

    path.write_bytes(path.read_bytes() + b"tamper")
    assert hashlib.sha256(path.read_bytes()).hexdigest() != manifest["dataset_sha256"]
