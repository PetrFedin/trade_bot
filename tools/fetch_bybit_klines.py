"""Fetch historical Bybit linear klines into a local dataset.

The frozen strategy record reports 137 target-first episodes against 471 stop-first,
and neither the dataset that produced it nor the code that computed it is in this
repository. Nothing can be reproduced, confirmed or refuted from what is committed.

This fetches public market data - no credentials, no account, read-only - so the
episode statistics can be recomputed from bars rather than carried forward from prose.
Each dataset is written next to a manifest recording the exact request that produced
it, so a later reader can tell what was fetched and re-fetch the same window.

Bars are written oldest-first. Bybit returns at most 1000 per request, newest-first,
and the paging here walks backwards from the end of the window.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "bybit"
ENDPOINT = "https://api.bybit.com/v5/market/kline"
PAGE_LIMIT = 1000

# Bybit interval codes mapped to the bar duration they represent.
INTERVALS = {
    "1": timedelta(minutes=1),
    "5": timedelta(minutes=5),
    "15": timedelta(minutes=15),
    "60": timedelta(hours=1),
    "240": timedelta(hours=4),
    "D": timedelta(days=1),
}


class FetchError(RuntimeError):
    """Raised when the venue refuses or the response is not the documented shape."""


@dataclass(frozen=True)
class Request:
    symbol: str
    interval: str
    start: datetime
    end: datetime

    def validate(self) -> None:
        if self.interval not in INTERVALS:
            raise ValueError(f"unsupported interval {self.interval!r}")
        if self.start >= self.end:
            raise ValueError("start must precede end")
        for name, value in (("start", self.start), ("end", self.end)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")


def _get(params: dict, *, retries: int = 4, pause: float = 0.25) -> list[list[str]]:
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
        result = payload.get("result") or {}
        rows = result.get("list")
        if not isinstance(rows, list):
            raise FetchError("response carried no kline list")
        return rows
    raise FetchError(f"request failed after {retries} attempts: {last}")


def fetch(request: Request, *, pause: float = 0.12) -> list[dict]:
    """Return every bar in the window, oldest first, without duplicates."""
    request.validate()
    start_ms = int(request.start.timestamp() * 1000)
    cursor_ms = int(request.end.timestamp() * 1000)
    by_timestamp: dict[int, dict] = {}

    while cursor_ms > start_ms:
        rows = _get(
            {
                "category": "linear",
                "symbol": request.symbol,
                "interval": request.interval,
                "start": start_ms,
                "end": cursor_ms,
                "limit": PAGE_LIMIT,
            }
        )
        if not rows:
            break
        oldest_ms = cursor_ms
        for row in rows:
            opened_ms = int(row[0])
            oldest_ms = min(oldest_ms, opened_ms)
            if opened_ms < start_ms:
                continue
            by_timestamp[opened_ms] = {
                "timestamp": datetime.fromtimestamp(opened_ms / 1000, tz=UTC).isoformat(),
                "symbol": request.symbol,
                "open": row[1],
                "high": row[2],
                "low": row[3],
                "close": row[4],
                "volume": row[5],
            }
        if oldest_ms >= cursor_ms:
            break
        cursor_ms = oldest_ms - 1
        time.sleep(pause)

    return [by_timestamp[key] for key in sorted(by_timestamp)]


def write_dataset(bars: list[dict], request: Request, directory: Path) -> Path:
    """Write bars and a manifest describing exactly how they were obtained."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request.symbol}_{request.interval}.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["timestamp", "symbol", "open", "high", "low", "close", "volume"]
        )
        writer.writeheader()
        writer.writerows(bars)
    manifest = {
        "schema_version": "bybit-kline-manifest-v1",
        "source_classification": "PUBLIC_VENUE_DATA_NON_AUTHORITATIVE",
        "endpoint": ENDPOINT,
        "category": "linear",
        "symbol": request.symbol,
        "interval": request.interval,
        "requested_start": request.start.isoformat(),
        "requested_end": request.end.isoformat(),
        "bar_count": len(bars),
        "first_bar": bars[0]["timestamp"] if bars else None,
        "last_bar": bars[-1]["timestamp"] if bars else None,
        "dataset_file": path.name,
        "limitations": (
            "Public venue data retrieved without authentication. Gaps, revisions and "
            "listing history are not asserted; the venue may omit bars with no trades."
        ),
    }
    (directory / f"{request.symbol}_{request.interval}.manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT"])
    parser.add_argument("--intervals", nargs="+", default=["D"])
    parser.add_argument("--days", type=int, default=365, help="window length ending now")
    parser.add_argument("--out", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)

    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    for symbol in args.symbols:
        for interval in args.intervals:
            request = Request(symbol=symbol, interval=interval, start=start, end=end)
            bars = fetch(request)
            path = write_dataset(bars, request, args.out)
            span = f"{bars[0]['timestamp'][:10]}..{bars[-1]['timestamp'][:10]}" if bars else "empty"
            print(f"{symbol:<10} {interval:>4}  {len(bars):>8} bars  {span}  -> {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
