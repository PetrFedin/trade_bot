"""Fail-closed admission of independently sourced intraday replay evidence.

This validator does NOT download, synthesize or resample intraday prices. It
requires exact external files and a declared provenance manifest. Approval for
actual trading is outside this module.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path


class IntradayAdmissionError(ValueError):
    pass


def validate_intraday_fixture(
    directory: Path, *, symbols: tuple[str, ...], days: tuple[str, ...],
    interval_minutes: int = 60, maximum_missing_fraction: float = 0.01,
) -> dict:
    if interval_minutes not in (1, 5, 15, 60):
        raise IntradayAdmissionError("unsupported intraday resolution")
    if not symbols or len(symbols) != len(set(symbols)):
        raise IntradayAdmissionError("duplicate or missing symbols")
    if not days or len(days) != len(set(days)):
        raise IntradayAdmissionError("duplicate or missing UTC dates")
    if not 0 <= maximum_missing_fraction <= 0.05:
        raise IntradayAdmissionError("missing fraction must be bounded")
    manifest_path = directory / "provenance.json"
    if not manifest_path.is_file():
        raise IntradayAdmissionError("declared provenance is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    origin = manifest.get("data_origin")
    expected_origins = {
        "BYBIT_V5_PUBLIC_KLINE": "https://api.bybit.com/v5/market/kline",
        "BYBIT_CONNECTOR_PUBLIC_KLINE": "plugin://Bybit/get_kline",
        "TEST_FIXTURE_ONLY": None,
    }
    if origin not in expected_origins:
        raise IntradayAdmissionError("unsupported or fabricated provenance claim")
    fixture_only = origin == "TEST_FIXTURE_ONLY"
    if not fixture_only:
        if (
            manifest.get("source_endpoint") != expected_origins[origin]
            or not manifest.get("collector")
            or not manifest.get("collected_at_utc")
        ):
            raise IntradayAdmissionError("real upstream provenance fields missing")
        try:
            collected_at = datetime.fromisoformat(
                manifest["collected_at_utc"].replace("Z", "+00:00")
            )
        except (ValueError, TypeError) as exc:
            raise IntradayAdmissionError("invalid provenance collection time") from exc
        if collected_at.tzinfo is None or collected_at.utcoffset() != timedelta(0):
            raise IntradayAdmissionError("provenance collection time must be UTC")
    if manifest.get("interval_minutes") != interval_minutes:
        raise IntradayAdmissionError("manifest interval mismatch")
    expected_per_day = 1440 // interval_minutes
    report = {}
    for symbol in symbols:
        path = directory / f"{symbol}_{interval_minutes}m.csv"
        claimed_digest = manifest.get("sha256", {}).get(path.name)
        if not path.is_file() or not isinstance(claimed_digest, str):
            raise IntradayAdmissionError("intraday CSV or pinned SHA missing")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if claimed_digest != digest:
            raise IntradayAdmissionError("intraday source SHA mismatch")
        rows_by_day = {day: set() for day in days}
        last_ts = None
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            required = {"timestamp", "open", "high", "low", "close", "volume"}
            if not required <= set(reader.fieldnames or ()):
                raise IntradayAdmissionError("intraday CSV schema mismatch")
            from decimal import Decimal
            for row in reader:
                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
                    raise IntradayAdmissionError("UTC timestamp required")
                if ts.minute % interval_minutes or ts.second or ts.microsecond:
                    raise IntradayAdmissionError("misaligned candle timestamp")
                if last_ts is not None and ts <= last_ts:
                    raise IntradayAdmissionError("nonmonotonic or duplicated intraday timestamp")
                last_ts = ts
                o, h, low, c, v = (Decimal(row[field]) for field in
                                  ("open", "high", "low", "close", "volume"))
                if not all(x.is_finite() for x in (o, h, low, c, v)):
                    raise IntradayAdmissionError("nonfinite OHLCV")
                invalid = (min(o, h, low, c) <= 0 or v < 0
                           or h < max(o, c, low) or low > min(o, c, h))
                if invalid:
                    raise IntradayAdmissionError("invalid OHLCV bounds")
                day = ts.date().isoformat()
                if day in rows_by_day:
                    rows_by_day[day].add(ts)
        for day, candles in rows_by_day.items():
            if len(candles) < expected_per_day * (1 - maximum_missing_fraction):
                raise IntradayAdmissionError(
                    f"incomplete intraday coverage: {symbol} {day}"
                )
        report[symbol] = {
            "sha256": digest,
            "days": len(days),
            "interval_minutes": interval_minutes,
        }
    return {
        "schema_validated": True,
        "admitted_for_research_only": not fixture_only,
        "test_fixture_only": fixture_only,
        "provider_authenticity_independently_verified": False,
        "source_claim_is_self_declared": True,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "source_provenance": origin,
        "instruments": report,
        "replay_completed": False,
        "profitability_proven": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--days", nargs="+", required=True)
    parser.add_argument("--interval-minutes", type=int, default=60)
    args = parser.parse_args(argv)
    result = validate_intraday_fixture(
        args.directory, symbols=tuple(args.symbols), days=tuple(args.days),
        interval_minutes=args.interval_minutes,
    )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
