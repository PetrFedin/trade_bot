from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from app.marketdata.bybit_funding import BybitFundingHistoryClient
from app.marketdata.bybit_public_archive import completed_archive_dates

_DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "ADAUSDT",
)


def acquire_funding_history(
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    lookback_days: int = 14,
    client: BybitFundingHistoryClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if lookback_days < 1:
        raise ValueError("funding acquisition lookback_days must be positive")
    cutoff = datetime.now(UTC) if now is None else now
    dates = completed_archive_dates(now=cutoff, lookback_days=lookback_days)
    start = datetime.combine(dates[0], time.min, tzinfo=UTC)
    end = datetime.combine(dates[-1] + timedelta(days=1), time.min, tzinfo=UTC)
    funding_client = BybitFundingHistoryClient() if client is None else client

    rows_by_symbol: dict[str, list[dict[str, object]]] = {}
    counts: dict[str, int] = {}
    request_counts: dict[str, int] = {}
    blocked: dict[str, str] = {}
    blocked_details: dict[str, str] = {}
    for symbol in symbols:
        try:
            history = funding_client.fetch_history(
                symbol=symbol,
                start_time=start,
                end_time=end,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            blocked[symbol] = type(exc).__name__
            blocked_details[symbol] = _safe_failure_detail(exc)
            continue
        rows = [
            {
                "funding_time": record.funding_time.isoformat(),
                "funding_rate": str(record.funding_rate),
            }
            for record in history.records
        ]
        rows_by_symbol[symbol] = rows
        counts[symbol] = len(rows)
        request_counts[symbol] = history.request_count

    qualification = (
        "PASS_BYBIT_FUNDING_HISTORY_ACQUISITION"
        if not blocked and set(rows_by_symbol) == set(symbols)
        else "BLOCKED_BYBIT_FUNDING_EXTERNAL_ACCESS"
    )
    return {
        "qualification": qualification,
        "source": "BYBIT_V5_PUBLIC_FUNDING_HISTORY",
        "symbols": list(symbols),
        "lookback_completed_utc_days": lookback_days,
        "archive_dates": [value.isoformat() for value in dates],
        "start_time": start.isoformat(),
        "end_time_exclusive": end.isoformat(),
        "records_by_symbol": rows_by_symbol,
        "record_counts_by_symbol": counts,
        "request_counts_by_symbol": request_counts,
        "blocked_symbols": blocked,
        "blocked_details_by_symbol": blocked_details,
        "mark_price_evidence_included": False,
        "funding_usdt_impact_calculated": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def _safe_failure_detail(exc: Exception) -> str:
    """Keep public-data diagnostics useful without dumping arbitrary exception payloads."""

    message = str(exc).strip()
    prefixes = (
        "Bybit funding-history HTTP status ",
        "Bybit funding-history retCode ",
        "Bybit funding-history returned invalid JSON",
        "Bybit funding-history response must be an object",
        "Bybit funding-history result must be an object",
        "Bybit funding-history result.list must be an array",
        "Bybit funding-history daily window reached API limit",
        "conflicting Bybit funding rates for one funding timestamp",
    )
    for prefix in prefixes:
        if message.startswith(prefix):
            return message[:160]
    if isinstance(exc, OSError):
        return f"NETWORK_{type(exc).__name__}"
    return type(exc).__name__


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Acquire read-only Bybit funding history")
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(
        symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()
    )
    report = acquire_funding_history(
        symbols=symbols,
        lookback_days=args.lookback_days,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_FUNDING_HISTORY=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
