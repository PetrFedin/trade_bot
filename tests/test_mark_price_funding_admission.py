"""Mark-price funding evidence admission and conservative envelope tests."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tools.mark_price_funding_admission import (
    FUNDING_SCHEMA,
    OFFICIAL_ENDPOINT,
    SCHEMA,
    MarkPriceAdmissionError,
    admit_mark_price_source,
    funding_cashflow_envelope,
)

D = Decimal
START = datetime(2026, 1, 1, tzinfo=UTC)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(root: Path, *, symbol: str = "BTCUSDT", count: int = 3):
    root.mkdir(parents=True, exist_ok=True)
    funding_csv = root / f"{symbol}_funding.csv"
    funding_manifest = root / f"{symbol}_funding.manifest.json"
    mark_csv = root / f"{symbol}_mark_1m.csv"
    mark_manifest = root / f"{symbol}_mark_1m.manifest.json"

    funding_rows = []
    mark_rows = []
    for index in range(count):
        stamp = START + timedelta(hours=8 * index)
        funding_rows.append((stamp, D("0.0001") if index != 1 else D("-0.0002")))
        mark_rows.append((
            stamp,
            D("100") + index,
            D("101") + index,
            D("99") + index,
            D("100.5") + index,
        ))

    with funding_csv.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "symbol", "funding_rate"])
        for stamp, rate in funding_rows:
            writer.writerow([stamp.isoformat().replace("+00:00", "Z"), symbol, str(rate)])

    funding_manifest.write_text(json.dumps({
        "schema_version": FUNDING_SCHEMA,
        "symbol": symbol,
        "category": "linear",
        "endpoint": "https://api.bytick.com/v5/market/funding/history",
        "dataset_sha256": _sha(funding_csv),
        "settlement_count": count,
        "first_settlement": funding_rows[0][0].isoformat().replace("+00:00", "Z"),
        "last_settlement": funding_rows[-1][0].isoformat().replace("+00:00", "Z"),
    }), encoding="utf-8")

    with mark_csv.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "symbol", "open", "high", "low", "close"])
        for stamp, open_, high, low, close in mark_rows:
            writer.writerow([
                stamp.isoformat().replace("+00:00", "Z"),
                symbol,
                str(open_),
                str(high),
                str(low),
                str(close),
            ])

    mark_manifest.write_text(json.dumps({
        "schema_version": SCHEMA,
        "symbol": symbol,
        "category": "linear",
        "interval_minutes": 1,
        "endpoint": OFFICIAL_ENDPOINT,
        "dataset_sha256": _sha(mark_csv),
        "research_only": True,
        "strategy_promotion_allowed": False,
    }), encoding="utf-8")
    return funding_csv, funding_manifest, mark_csv, mark_manifest


def test_long_positive_funding_is_negative_cash_interval() -> None:
    result = funding_cashflow_envelope(
        quantity=D("2"),
        funding_rate=D("0.001"),
        mark_low=D("99"),
        mark_high=D("101"),
        side="LONG",
    )
    assert result.minimum_cashflow == D("-0.202")
    assert result.maximum_cashflow == D("-0.198")


def test_long_negative_funding_is_positive_cash_interval() -> None:
    result = funding_cashflow_envelope(
        quantity=D("2"),
        funding_rate=D("-0.001"),
        mark_low=D("99"),
        mark_high=D("101"),
        side="LONG",
    )
    assert result.minimum_cashflow == D("0.198")
    assert result.maximum_cashflow == D("0.202")


def test_short_reverses_cash_sign() -> None:
    long = funding_cashflow_envelope(
        quantity=D("3"), funding_rate=D("0.001"),
        mark_low=D("99"), mark_high=D("101"), side="LONG",
    )
    short = funding_cashflow_envelope(
        quantity=D("3"), funding_rate=D("0.001"),
        mark_low=D("99"), mark_high=D("101"), side="SHORT",
    )
    assert short.minimum_cashflow == -long.maximum_cashflow
    assert short.maximum_cashflow == -long.minimum_cashflow


def test_invalid_envelope_inputs_fail_closed() -> None:
    with pytest.raises(MarkPriceAdmissionError, match="positive and ordered"):
        funding_cashflow_envelope(
            quantity=D("0"), funding_rate=D("0.001"),
            mark_low=D("99"), mark_high=D("101"),
        )
    with pytest.raises(MarkPriceAdmissionError, match="side"):
        funding_cashflow_envelope(
            quantity=D("1"), funding_rate=D("0.001"),
            mark_low=D("99"), mark_high=D("101"), side="UNKNOWN",
        )


def test_exact_settlement_minute_coverage_admitted_but_not_exact_mark(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    report = admit_mark_price_source(
        symbol="BTCUSDT",
        funding_csv=paths[0],
        funding_manifest=paths[1],
        mark_csv=paths[2],
        mark_manifest=paths[3],
    )
    assert report["settlements"] == 3
    assert report["all_funding_timestamps_have_exact_minute_mark_candle"] is True
    assert report["mark_price_exact_at_settlement_verified"] is False
    assert report["account_funding_ledger_verified"] is False
    assert report["strategy_replay_completed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["profitability_proven"] is False
    assert all(row["one_minute_envelope_only"] for row in report["rows"])


def test_missing_mark_settlement_rejected(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    rows = paths[2].read_text(encoding="utf-8").splitlines()
    paths[2].write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")
    meta = json.loads(paths[3].read_text(encoding="utf-8"))
    meta["dataset_sha256"] = _sha(paths[2])
    paths[3].write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(MarkPriceAdmissionError, match="timestamp identity mismatch"):
        admit_mark_price_source(
            symbol="BTCUSDT",
            funding_csv=paths[0],
            funding_manifest=paths[1],
            mark_csv=paths[2],
            mark_manifest=paths[3],
        )


def test_extra_mark_minute_rejected(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    with paths[2].open("a", encoding="utf-8") as stream:
        stamp = START + timedelta(hours=24)
        stream.write(
            f"{stamp.isoformat().replace('+00:00', 'Z')},BTCUSDT,103,104,102,103.5\n"
        )
    meta = json.loads(paths[3].read_text(encoding="utf-8"))
    meta["dataset_sha256"] = _sha(paths[2])
    paths[3].write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(MarkPriceAdmissionError, match="timestamp identity mismatch"):
        admit_mark_price_source(
            symbol="BTCUSDT",
            funding_csv=paths[0],
            funding_manifest=paths[1],
            mark_csv=paths[2],
            mark_manifest=paths[3],
        )


def test_tampered_mark_source_rejected(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths[2].write_text(
        paths[2].read_text(encoding="utf-8").replace("101,99", "102,99", 1),
        encoding="utf-8",
    )
    with pytest.raises(MarkPriceAdmissionError, match="manifest/source"):
        admit_mark_price_source(
            symbol="BTCUSDT",
            funding_csv=paths[0],
            funding_manifest=paths[1],
            mark_csv=paths[2],
            mark_manifest=paths[3],
        )


def test_last_traded_or_daily_mark_substitution_rejected(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    meta = json.loads(paths[3].read_text(encoding="utf-8"))
    meta["endpoint"] = "https://api.bybit.com/v5/market/kline"
    paths[3].write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(MarkPriceAdmissionError, match="manifest/source"):
        admit_mark_price_source(
            symbol="BTCUSDT",
            funding_csv=paths[0],
            funding_manifest=paths[1],
            mark_csv=paths[2],
            mark_manifest=paths[3],
        )


def test_one_minute_interval_is_mandatory(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    meta = json.loads(paths[3].read_text(encoding="utf-8"))
    meta["interval_minutes"] = 5
    paths[3].write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(MarkPriceAdmissionError, match="manifest/source"):
        admit_mark_price_source(
            symbol="BTCUSDT",
            funding_csv=paths[0],
            funding_manifest=paths[1],
            mark_csv=paths[2],
            mark_manifest=paths[3],
        )


def test_official_bytick_mainnet_host_is_accepted(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    meta = json.loads(paths[3].read_text(encoding="utf-8"))
    meta["endpoint"] = "https://api.bytick.com/v5/market/mark-price-kline"
    paths[3].write_text(json.dumps(meta), encoding="utf-8")
    report = admit_mark_price_source(
        symbol="BTCUSDT",
        funding_csv=paths[0],
        funding_manifest=paths[1],
        mark_csv=paths[2],
        mark_manifest=paths[3],
    )
    assert report["mark_endpoint_claim"] == meta["endpoint"]
    assert report["mark_price_exact_at_settlement_verified"] is False
