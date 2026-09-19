"""Fetch historical funding rates for Bybit linear perpetuals.

Every measurement in this research priced entries, exits and fees and ignored funding.
On a perpetual that is not a rounding error. Funding settles every eight hours, and a
position held for ten daily bars sits through roughly thirty settlements. At the rates
these instruments actually printed that is the same order of magnitude as the entire
measured alpha, so leaving it out does not make the numbers slightly optimistic - it
makes them unreliable.

A positive rate means longs pay shorts, which is the ordinary state of a crypto
perpetual and the wrong side for a long-only strategy to be standing on.

Public endpoint, no credentials, read-only. Each dataset is written beside a manifest
recording the request that produced it.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "bybit_funding"
ENDPOINT = "https://api.bybit.com/v5/market/funding/history"
PAGE_LIMIT = 200
SETTLEMENT_HOURS = 8


class FetchError(RuntimeError):
    """Raised when the venue refuses or the response is not the documented shape."""


def _get(params: dict, *, retries: int = 4, pause: float = 0.25) -> list[dict]:
    query = urllib.parse.urlencode(params)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(f"{ENDPOINT}?{query}", timeout=30) as response:
                payload = json.load(response)
        except (urllib.error.URLError, TimeoutError) as error:
            last = error
            time.sleep(pause * (2**attempt))
            continue
        if payload.get("retCode") != 0:
            raise FetchError(f"venue refused: {payload.get('retCode')} {payload.get('retMsg')}")
        rows = (payload.get("result") or {}).get("list")
        if not isinstance(rows, list):
            raise FetchError("response carried no funding list")
        return rows
    raise FetchError(f"request failed after {retries} attempts: {last}")


def fetch(symbol: str, start: datetime, end: datetime, *, pause: float = 0.12) -> list[dict]:
    """Return every settlement in the window, oldest first, without duplicates."""
    start_ms = int(start.timestamp() * 1000)
    cursor_ms = int(end.timestamp() * 1000)
    by_timestamp: dict[int, dict] = {}

    while cursor_ms > start_ms:
        try:
            rows = _get(
                {
                    "category": "linear",
                    "symbol": symbol,
                    "startTime": start_ms,
                    "endTime": cursor_ms,
                    "limit": PAGE_LIMIT,
                }
            )
        except FetchError:
            # Paging past an instrument's listing date is refused rather than answered
            # with an empty page. Whatever was already collected is real history and is
            # worth more than an exception.
            break
        if not rows:
            break
        oldest = cursor_ms
        for row in rows:
            settled = int(row["fundingRateTimestamp"])
            oldest = min(oldest, settled)
            if settled < start_ms:
                continue
            by_timestamp[settled] = {
                "timestamp": datetime.fromtimestamp(settled / 1000, tz=UTC).isoformat(),
                "symbol": symbol,
                "funding_rate": row["fundingRate"],
            }
        if oldest >= cursor_ms:
            break
        cursor_ms = oldest - 1
        time.sleep(pause)

    return [by_timestamp[key] for key in sorted(by_timestamp)]


def write_dataset(rows: list[dict], symbol: str, start: datetime, end: datetime, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{symbol}_funding.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "symbol", "funding_rate"])
        writer.writeheader()
        writer.writerows(rows)
    rates = [float(row["funding_rate"]) for row in rows]
    manifest = {
        "schema_version": "bybit-funding-manifest-v1",
        "source_classification": "PUBLIC_VENUE_DATA_NON_AUTHORITATIVE",
        "endpoint": ENDPOINT,
        "category": "linear",
        "symbol": symbol,
        "settlement_hours": SETTLEMENT_HOURS,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "settlement_count": len(rows),
        "mean_rate": round(sum(rates) / len(rates), 10) if rates else None,
        "positive_share": round(sum(1 for r in rates if r > 0) / len(rates), 6) if rates else None,
        "dataset_file": path.name,
        "limitations": (
            "A positive rate means longs pay shorts. Settlements with no printed rate "
            "are absent rather than zero, and the venue may revise history."
        ),
    }
    (out / f"{symbol}_funding.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--days", type=int, default=1095)
    parser.add_argument("--out", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)

    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    failures = 0
    for symbol in args.symbols:
        try:
            rows = fetch(symbol, start, end)
        except FetchError as error:
            failures += 1
            print(f"{symbol:<10} skipped: {error}")
            continue
        if not rows:
            failures += 1
            print(f"{symbol:<10} skipped: no settlements returned")
            continue
        path = write_dataset(rows, symbol, start, end, args.out)
        rates = [float(row["funding_rate"]) for row in rows]
        mean = sum(rates) / len(rates) if rates else 0.0
        print(
            f"{symbol:<10} {len(rows):>6} settlements  mean {mean:+.6%} per {SETTLEMENT_HOURS}h "
            f"({mean * 3 * 365:+.2%} annualised)  -> {path.name}"
        )
    if failures:
        print(f"\n{failures} symbol(s) produced no usable history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
