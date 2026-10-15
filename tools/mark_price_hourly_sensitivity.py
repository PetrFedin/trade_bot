"""Collect Bybit public 60m Mark Price source for funding sensitivity.

This is a scalable coarse envelope, NOT the final one-minute admission from
PR #325 and not an account-authoritative funding statement. It never falls
back to traded-price klines. Raw hourly source, query-page digests and funding
settlement intersections are preserved for review.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests

D = Decimal
HOUR = timedelta(hours=1)
HOUR_MS = 3_600_000
LIMIT = 1000
URL = "https://api.bytick.com/v5/market/mark-price-kline"
FUNDING_SCHEMA = "bybit-funding-snapshot-v2"


class HourlyMarkSourceError(ValueError):
    pass


@dataclass(frozen=True)
class MarkHour:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def validate(self) -> None:
        values = (self.open, self.high, self.low, self.close)
        if (
            not all(value.is_finite() and value > 0 for value in values)
            or self.high < max(values)
            or self.low > min(values)
        ):
            raise HourlyMarkSourceError("invalid Mark Price OHLC")


def _utc(text: str, label: str) -> datetime:
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise HourlyMarkSourceError(f"invalid {label}") from exc
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise HourlyMarkSourceError(f"{label} must be explicit UTC")
    return value.astimezone(UTC)


def _decimal(text: str, label: str) -> Decimal:
    try:
        value = D(text)
    except (InvalidOperation, TypeError) as exc:
        raise HourlyMarkSourceError(f"invalid {label}") from exc
    if not value.is_finite():
        raise HourlyMarkSourceError(f"{label} must be finite")
    return value


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _aligned_hour(value: datetime, label: str) -> datetime:
    value = value.astimezone(UTC)
    if value.minute or value.second or value.microsecond:
        raise HourlyMarkSourceError(f"{label} must align to UTC hour")
    return value


def _parse_page(payload: dict, *, symbol: str) -> tuple[MarkHour, ...]:
    if payload.get("retCode") != 0:
        raise HourlyMarkSourceError(
            f"Bybit returned retCode={payload.get('retCode')}"
        )
    result = payload.get("result")
    if (
        not isinstance(result, dict)
        or result.get("category") != "linear"
        or result.get("symbol") != symbol
        or not isinstance(result.get("list"), list)
    ):
        raise HourlyMarkSourceError("unexpected Bybit Mark Price response")
    parsed = []
    for row in result["list"]:
        if not isinstance(row, list) or len(row) < 5:
            raise HourlyMarkSourceError("malformed Mark Price row")
        stamp = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
        stamp = _aligned_hour(stamp, "mark timestamp")
        mark = MarkHour(
            timestamp=stamp,
            open=_decimal(row[1], "mark open"),
            high=_decimal(row[2], "mark high"),
            low=_decimal(row[3], "mark low"),
            close=_decimal(row[4], "mark close"),
        )
        mark.validate()
        parsed.append(mark)
    if len({row.timestamp for row in parsed}) != len(parsed):
        raise HourlyMarkSourceError("duplicate Mark Price hour")
    return tuple(sorted(parsed, key=lambda row: row.timestamp))


def fetch_hourly_marks(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    get: Callable[..., Any] = requests.get,
    pause_seconds: float = 0.08,
) -> tuple[tuple[MarkHour, ...], tuple[dict, ...]]:
    start = _aligned_hour(start, "start")
    end = _aligned_hour(end, "end")
    if end < start:
        raise HourlyMarkSourceError("end precedes start")
    if not symbol or symbol != symbol.upper():
        raise HourlyMarkSourceError("symbol must be uppercase")
    rows: dict[datetime, MarkHour] = {}
    pages = []
    cursor = start
    page_number = 0
    while cursor <= end:
        segment_end = min(end, cursor + HOUR * (LIMIT - 1))
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": "60",
            "start": int(cursor.timestamp() * 1000),
            "end": int(segment_end.timestamp() * 1000),
            "limit": LIMIT,
        }
        last_error: Exception | None = None
        payload = None
        for attempt in range(5):
            try:
                response = get(URL, params=params, timeout=30)
                response.raise_for_status()
                payload = response.json()
                break
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == 4:
                    raise HourlyMarkSourceError(
                        "Bybit Mark Price request failed after retries"
                    ) from exc
                time.sleep(min(2.0, 0.2 * (2**attempt)))
        if payload is None:
            raise HourlyMarkSourceError("missing Bybit response") from last_error
        parsed = _parse_page(payload, symbol=symbol)
        expected_count = int((segment_end - cursor) / HOUR) + 1
        expected = tuple(cursor + HOUR * offset for offset in range(expected_count))
        observed = tuple(row.timestamp for row in parsed)
        if observed != expected:
            raise HourlyMarkSourceError(
                f"hourly continuity mismatch page={page_number} "
                f"expected={len(expected)} observed={len(observed)}"
            )
        for row in parsed:
            rows[row.timestamp] = row
        pages.append({
            "page": page_number,
            "query": params,
            "provider_result_sha256": _sha_bytes(
                _canonical(payload.get("result"))
            ),
            "rows": len(parsed),
            "first_hour": parsed[0].timestamp.isoformat(),
            "last_hour": parsed[-1].timestamp.isoformat(),
        })
        page_number += 1
        cursor = segment_end + HOUR
        if pause_seconds:
            time.sleep(pause_seconds)
    ordered = tuple(rows[stamp] for stamp in sorted(rows))
    expected_total = int((end - start) / HOUR) + 1
    if len(ordered) != expected_total:
        raise HourlyMarkSourceError("full Mark Price chronology incomplete")
    return ordered, tuple(pages)


def load_funding(
    *, symbol: str, funding_csv: Path, funding_manifest: Path
) -> tuple[tuple[datetime, Decimal], ...]:
    manifest = json.loads(funding_manifest.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != FUNDING_SCHEMA
        or manifest.get("symbol") != symbol
        or manifest.get("category") != "linear"
        or manifest.get("dataset_sha256") != _sha_file(funding_csv)
    ):
        raise HourlyMarkSourceError("funding source manifest mismatch")
    output = []
    with funding_csv.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["timestamp", "symbol", "funding_rate"]:
            raise HourlyMarkSourceError("unexpected funding source schema")
        previous = None
        for row in reader:
            if row["symbol"] != symbol:
                raise HourlyMarkSourceError("funding symbol contamination")
            stamp = _utc(row["timestamp"], "funding timestamp")
            stamp = _aligned_hour(stamp, "funding timestamp")
            rate = _decimal(row["funding_rate"], "funding rate")
            if previous is not None and stamp <= previous:
                raise HourlyMarkSourceError("funding timestamps reordered")
            previous = stamp
            output.append((stamp, rate))
    if len(output) != manifest.get("settlement_count"):
        raise HourlyMarkSourceError("funding settlement count mismatch")
    return tuple(output)


def collect_symbol(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    funding_csv: Path,
    funding_manifest: Path,
    output_dir: Path,
    get: Callable[..., Any] = requests.get,
    pause_seconds: float = 0.08,
) -> dict:
    marks, pages = fetch_hourly_marks(
        symbol=symbol,
        start=start,
        end=end,
        get=get,
        pause_seconds=pause_seconds,
    )
    funding = tuple(
        (stamp, rate)
        for stamp, rate in load_funding(
            symbol=symbol,
            funding_csv=funding_csv,
            funding_manifest=funding_manifest,
        )
        if start <= stamp <= end
    )
    mark_by_time = {row.timestamp: row for row in marks}
    if not funding:
        raise HourlyMarkSourceError("no funding settlements inside collection window")
    missing = [stamp for stamp, _ in funding if stamp not in mark_by_time]
    if missing:
        raise HourlyMarkSourceError(
            f"funding settlements missing Mark Price hour: {len(missing)}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / f"{symbol}_mark_60m.csv"
    with raw_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "symbol", "open", "high", "low", "close"])
        for row in marks:
            writer.writerow([
                row.timestamp.isoformat(),
                symbol,
                row.open,
                row.high,
                row.low,
                row.close,
            ])
    settlement_path = output_dir / f"{symbol}_funding_mark_60m.csv"
    widths = []
    with settlement_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "timestamp", "symbol", "funding_rate",
            "mark_open", "mark_high", "mark_low", "mark_close",
            "hour_range_fraction",
        ])
        for stamp, rate in funding:
            mark = mark_by_time[stamp]
            midpoint = (mark.high + mark.low) / D("2")
            width = (mark.high - mark.low) / midpoint
            widths.append(width)
            writer.writerow([
                stamp.isoformat(), symbol, rate,
                mark.open, mark.high, mark.low, mark.close, width,
            ])
    manifest = {
        "schema_version": "bybit-mark-price-hourly-sensitivity-v1",
        "evaluation_class": "COARSE_PUBLIC_MARK_PRICE_ENVELOPE_RESEARCH_ONLY",
        "symbol": symbol,
        "category": "linear",
        "endpoint": URL,
        "interval_minutes": 60,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hourly_rows": len(marks),
        "funding_settlements": len(funding),
        "raw_mark_sha256": _sha_file(raw_path),
        "settlement_mark_sha256": _sha_file(settlement_path),
        "funding_source_sha256": _sha_file(funding_csv),
        "funding_manifest_sha256": _sha_file(funding_manifest),
        "mean_settlement_hour_range_fraction": str(
            sum(widths, D("0")) / D(len(widths))
        ),
        "maximum_settlement_hour_range_fraction": str(max(widths)),
        "provider_query_pages": list(pages),
        "provider_payload_signature_verified": False,
        "exact_settlement_mark_verified": False,
        "account_funding_ledger_verified": False,
        "compatible_with_final_1m_admission": False,
        "strategy_replay_completed": False,
        "strategy_promotion_allowed": False,
        "profitability_proven": False,
        "limitations": [
            "60m OHLC only bounds the possible Mark Price at settlement hour",
            "Final PR #325 admission still requires 1m source at each settlement",
            "Public endpoint response is not an account funding transaction record",
            "No Last Price or entry-price substitution is permitted",
        ],
    }
    manifest_path = output_dir / f"{symbol}_mark_60m.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--funding-csv", type=Path, required=True)
    parser.add_argument("--funding-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pause-seconds", type=float, default=0.08)
    args = parser.parse_args()
    report = collect_symbol(
        symbol=args.symbol,
        start=_utc(args.start_utc, "start"),
        end=_utc(args.end_utc, "end"),
        funding_csv=args.funding_csv,
        funding_manifest=args.funding_manifest,
        output_dir=args.output_dir,
        pause_seconds=args.pause_seconds,
    )
    print("MARK_PRICE_HOURLY_SENSITIVITY", json.dumps({
        "symbol": report["symbol"],
        "hours": report["hourly_rows"],
        "funding_settlements": report["funding_settlements"],
        "max_hour_range_fraction": report[
            "maximum_settlement_hour_range_fraction"
        ],
        "exact_mark_verified": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
