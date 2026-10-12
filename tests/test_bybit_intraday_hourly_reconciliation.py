"""Provider-connector hourly data must reconstruct the frozen daily source exactly.

This is a data provenance and cross-resolution consistency test on an inspected
historical interval. It is not historical trade execution evidence or a holdout.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from tools.intraday_evidence_admission import validate_intraday_fixture

HOURLY = Path("research/snapshots/bybit_intraday_2025-07-01_2025-07-04_60m")
DAILY = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
SYMBOLS = ("BTCUSDT", "ETHUSDT")
DAYS = ("2025-07-01", "2025-07-02", "2025-07-03", "2025-07-04")


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_provider_snapshot_is_declared_but_not_independently_attested() -> None:
    provenance = json.loads((HOURLY / "provenance.json").read_text(encoding="utf-8"))
    report = validate_intraday_fixture(
        HOURLY,
        symbols=SYMBOLS,
        days=DAYS,
        interval_minutes=60,
        maximum_missing_fraction=0,
    )
    assert report["schema_validated"] is True
    assert report["admitted_for_research_only"] is True
    assert report["test_fixture_only"] is False
    assert report["provider_authenticity_independently_verified"] is False
    assert report["source_claim_is_self_declared"] is True
    assert report["strategy_promotion_allowed"] is False
    assert report["profitability_proven"] is False
    assert provenance["data_origin"] == "BYBIT_CONNECTOR_PUBLIC_KLINE"
    assert provenance["source_endpoint"] == "plugin://Bybit/get_kline"
    assert provenance["evaluation_class"] == (
        "HISTORICAL_INTRADAY_DATA_QUALITY_AND_EXECUTION_RECONSTRUCTION_ONLY"
    )
    assert provenance["symbols"] == list(SYMBOLS)


def test_hourly_ohlcv_reconciles_exactly_to_frozen_daily_bars() -> None:
    reconciled_days = 0
    for symbol in SYMBOLS:
        source = HOURLY / f"{symbol}_60m.csv"
        provenance = json.loads(
            (HOURLY / "provenance.json").read_text(encoding="utf-8")
        )
        assert hashlib.sha256(source.read_bytes()).hexdigest() == (
            provenance["sha256"][source.name]
        )
        hours_by_day: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in _rows(source):
            hours_by_day[row["timestamp"][:10]].append(row)
        frozen_by_day = {
            row["timestamp"][:10]: row
            for row in _rows(DAILY / f"{symbol}_D.csv")
        }
        for day in DAYS:
            hourly = hours_by_day[day]
            assert len(hourly) == 24
            frozen = frozen_by_day[day]
            actual = {
                "open": Decimal(hourly[0]["open"]),
                "high": max(Decimal(bar["high"]) for bar in hourly),
                "low": min(Decimal(bar["low"]) for bar in hourly),
                "close": Decimal(hourly[-1]["close"]),
                "volume": sum(
                    (Decimal(bar["volume"]) for bar in hourly), Decimal("0")
                ),
            }
            for field, observed in actual.items():
                assert observed == Decimal(frozen[field]), (symbol, day, field)
            reconciled_days += 1
    assert reconciled_days == 8


def test_hourly_source_does_not_touch_preregistered_holdout() -> None:
    for symbol in SYMBOLS:
        rows = _rows(HOURLY / f"{symbol}_60m.csv")
        assert rows[0]["timestamp"].startswith("2025-07-01")
        assert rows[-1]["timestamp"].startswith("2025-07-04")
        assert all(
            row["timestamp"] < "2026-10-11T00:00:00"
            for row in rows
        )
