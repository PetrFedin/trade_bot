"""Fail-closed research scenarios from six public Bybit book observations.

Snapshots are 2026 observations, NEVER retrofitted to earlier market trades.
All quotes are hypothetical and cannot authorize orders or funding settlement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.execution.market_depth_quote import (
    DepthSnapshot,
    InstrumentRules,
    QuoteRejected,
    quote_market_cross,
)

D = Decimal
SIX = ("BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
CAPITAL = (D("10000"), D("100000"), D("1000000"))
FEES = (D("8"), D("10"))
HAIRCUTS = (D("0"), D("0.5"))
POSITION_FRACTION = D("0.29")
SCHEMA = "astra-bybit-six-readonly-orderbook-observation-v1"


def canonical(data: object) -> bytes:
    return json.dumps(
        data, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def hash_value(data: object) -> str:
    return hashlib.sha256(canonical(data)).hexdigest()


def build_depth_stress(source: dict) -> dict:
    """144 visible-depth estimates, never historical or executable fills."""
    if (
        not isinstance(source, dict)
        or source.get("schema_version") != SCHEMA
        or source.get("market_category") != "linear"
        or source.get("source") != "BYBIT_PUBLIC_READONLY_PLUGIN_OBSERVATION"
        or source.get("provider_signed") is not False
        or source.get("historical_orderbook_observed") is not False
        or source.get("actual_fills_verified") is not False
        or source.get("live_trading_allowed") is not False
    ):
        raise QuoteRejected("invalid or unsafe public orderbook research source")
    observations = source.get("observations")
    if not isinstance(observations, dict) or tuple(sorted(observations)) != SIX:
        raise QuoteRejected("incomplete six-instrument public source")
    embedded_digest = source.get("observation_sha256")
    original = {k: v for k, v in source.items() if k != "observation_sha256"}
    calculated_digest = hash_value(original)
    if embedded_digest is not None and (
        not isinstance(embedded_digest, str)
        or embedded_digest != calculated_digest
    ):
        raise QuoteRejected("unverified public observation content hash")
    # This is a Git-revision-linked byte checksum, NOT exchange authentication.
    digest = calculated_digest
    timestamps = []
    rows = []
    for symbol in SIX:
        observation = observations[symbol]
        if not isinstance(observation, dict):
            raise QuoteRejected("missing per-symbol source bundle")
        book_raw = observation.get("book")
        instrument_raw = observation.get("instrument")
        if not isinstance(book_raw, dict):
            raise QuoteRejected("missing orderbook")
        stamp = book_raw.get("ts")
        rules = InstrumentRules.parse(instrument_raw, symbol)
        book = DepthSnapshot.parse(book_raw, rules, observation_ms=stamp)
        timestamps.append(stamp)
        for capital in CAPITAL:
            for fee in FEES:
                for haircut in HAIRCUTS:
                    for side in ("BUY", "SELL"):
                        quote = quote_market_cross(
                            book, rules, side=side,
                            requested_notional=capital * POSITION_FRACTION,
                            fee_bps=fee, depth_haircut=haircut,
                        )
                        if quote["hypothetical_fill_confirmed"] is not False:
                            raise QuoteRejected("research crossed executable authority")
                        rows.append({
                            "hypothetical_capital_usdt": str(capital),
                            "target_fraction": str(POSITION_FRACTION),
                            "execution": quote,
                        })
    if max(timestamps) - min(timestamps) > 5000:
        raise QuoteRejected("public book observations are not synchronized")
    if len(rows) != 144:
        raise QuoteRejected("incomplete fee/size/depth stress grid")
    full = sum(
        row["execution"]["status"] == "VISIBLE_DEPTH_SUFFICIENT"
        for row in rows
    )
    partial = sum(
        row["execution"]["status"] == "INSUFFICIENT_VISIBLE_DEPTH"
        for row in rows
    )
    filtered = len(rows) - full - partial
    result = {
        "schema_version": "astra-public-depth-execution-stress-v1",
        "evaluation_class": "CURRENT_PUBLIC_VISIBLE_DEPTH_PROXY_NOT_HISTORICAL",
        "source_observation_sha256": digest,
        "source_provider_signed": False,
        "snapshot_source_authenticated": False,
        "historical_execution_data_available": False,
        "venue_fills_verified": False,
        "fee_tier_verified": False,
        "mark_price_settlement_verified": False,
        "book_queue_position_verified": False,
        "live_trading_allowed": False,
        "strategy_promotion_allowed": False,
        "profitability_proven": False,
        "earliest_exchange_timestamp_utc": datetime.fromtimestamp(
            min(timestamps) / 1000, UTC
        ).isoformat(),
        "latest_exchange_timestamp_utc": datetime.fromtimestamp(
            max(timestamps) / 1000, UTC
        ).isoformat(),
        "cost_scenarios_bps_per_side": [str(x) for x in FEES],
        "visible_depth_haircut_assumptions": [str(x) for x in HAIRCUTS],
        "capital_scenarios_usdt": [str(x) for x in CAPITAL],
        "symbol_count": len(SIX),
        "quotes": len(rows),
        "displayed_depth_sufficient": full,
        "insufficient_displayed_depth": partial,
        "instrument_filter_rejections": filtered,
        "rows": rows,
        "limitations": [
            "Public book levels may disappear; NO execution guarantee",
            "Only ten visible depth levels supplied per side",
            "50% depth haircut is a stress assumption, NOT observed hidden depth",
            "8 and 10 bps fee rates are hypotheses, NOT account-specific fees",
            "NO historical orderbook: do not use this snapshot in 2023-2026 tests",
            "NO authenticated funding-settlement Mark Price or account ledger",
            "NO latency, maker queue, liquidation or market-impact forecast",
            "Capital size is hypothetical; no strategy optimization/promotion",
        ],
    }
    result["report_sha256"] = hash_value(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.source.is_file() or args.source.is_symlink():
        raise QuoteRejected("unsafe missing or symlinked public snapshot source")
    if args.output.exists() or args.output.is_symlink():
        raise QuoteRejected("refusing to overwrite execution-stress evidence")
    source = json.loads(args.source.read_text(encoding="utf-8"))
    report = build_depth_stress(source)
    from tools.publish_research_receipt import publish_new_json_receipt

    publish_new_json_receipt(args.output, report)
    print("VISIBLE_DEPTH_STRESS", json.dumps({
        "quotes": report["quotes"],
        "fully_visible": report["displayed_depth_sufficient"],
        "insufficient": report["insufficient_displayed_depth"],
        "instrument_rejected": report["instrument_filter_rejections"],
        "digest": report["report_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
