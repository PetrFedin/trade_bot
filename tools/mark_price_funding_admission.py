"""Research admission for historical mark-price funding evidence.

Bybit USDT perpetual funding uses position quantity * mark price * funding rate.
This module refuses to substitute last-traded or entry prices. It admits only
explicit 1-minute mark-price candles aligned to retained funding settlements and
produces a conservative cashflow envelope from the candle low/high. A passing
report is NOT an account-authoritative funding statement, exchange fill, or
profitability verdict.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

D = Decimal
ZERO = D("0")
SCHEMA = "bybit-mark-price-settlement-snapshot-v1"
FUNDING_SCHEMA = "bybit-funding-snapshot-v2"
OFFICIAL_ENDPOINT = "https://api.bybit.com/v5/market/mark-price-kline"


class MarkPriceAdmissionError(ValueError):
    pass


@dataclass(frozen=True)
class FundingMarkEnvelope:
    minimum_cashflow: Decimal
    maximum_cashflow: Decimal

    def validate(self) -> None:
        if (
            not self.minimum_cashflow.is_finite()
            or not self.maximum_cashflow.is_finite()
            or self.minimum_cashflow > self.maximum_cashflow
        ):
            raise MarkPriceAdmissionError("invalid funding cashflow envelope")


def _utc(text: str, label: str) -> datetime:
    if not isinstance(text, str):
        raise MarkPriceAdmissionError(f"{label} must be UTC ISO string")
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarkPriceAdmissionError(f"invalid {label}") from exc
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise MarkPriceAdmissionError(f"{label} must explicitly identify UTC")
    return value.astimezone(UTC)


def _decimal(text: str, label: str) -> Decimal:
    try:
        value = D(text)
    except (InvalidOperation, TypeError) as exc:
        raise MarkPriceAdmissionError(f"invalid {label}") from exc
    if not value.is_finite():
        raise MarkPriceAdmissionError(f"{label} must be finite")
    return value


def _sha(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise MarkPriceAdmissionError(f"source missing or symlinked: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def funding_cashflow_envelope(
    *,
    quantity: Decimal,
    funding_rate: Decimal,
    mark_low: Decimal,
    mark_high: Decimal,
    side: str = "LONG",
) -> FundingMarkEnvelope:
    """Return signed account-cash interval under 1m mark-price uncertainty.

    Cash sign is from the strategy account perspective:
      positive = cash credit, negative = cash payment.
    For a long, positive funding is a payment and negative funding a credit.
    For a short, the sign is reversed.
    """
    if side not in {"LONG", "SHORT"}:
        raise MarkPriceAdmissionError("side must be LONG or SHORT")
    for name, value in (
        ("quantity", quantity),
        ("funding_rate", funding_rate),
        ("mark_low", mark_low),
        ("mark_high", mark_high),
    ):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise MarkPriceAdmissionError(f"{name} must be finite Decimal")
    if quantity <= 0 or mark_low <= 0 or mark_high <= 0 or mark_low > mark_high:
        raise MarkPriceAdmissionError("quantity and mark prices must be positive and ordered")
    payer_sign = D("-1") if side == "LONG" else D("1")
    low_value = quantity * mark_low * funding_rate * payer_sign
    high_value = quantity * mark_high * funding_rate * payer_sign
    result = FundingMarkEnvelope(
        minimum_cashflow=min(low_value, high_value),
        maximum_cashflow=max(low_value, high_value),
    )
    result.validate()
    return result


def _load_funding(funding_csv: Path, funding_manifest: Path, symbol: str):
    meta = json.loads(funding_manifest.read_text(encoding="utf-8"))
    if (
        meta.get("schema_version") != FUNDING_SCHEMA
        or meta.get("symbol") != symbol
        or meta.get("category") != "linear"
        or meta.get("dataset_sha256") != _sha(funding_csv)
    ):
        raise MarkPriceAdmissionError("funding manifest/source mismatch")
    rows = []
    with funding_csv.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["timestamp", "symbol", "funding_rate"]:
            raise MarkPriceAdmissionError("unexpected funding CSV schema")
        previous = None
        for row in reader:
            if row["symbol"] != symbol:
                raise MarkPriceAdmissionError("funding symbol contamination")
            stamp = _utc(row["timestamp"], "funding timestamp")
            rate = _decimal(row["funding_rate"], "funding rate")
            if previous is not None and stamp <= previous:
                raise MarkPriceAdmissionError("funding timestamps duplicated or reordered")
            previous = stamp
            rows.append((stamp, rate))
    if len(rows) != meta.get("settlement_count"):
        raise MarkPriceAdmissionError("funding settlement count mismatch")
    if not rows:
        raise MarkPriceAdmissionError("empty funding source")
    if rows[0][0] != _utc(meta["first_settlement"], "first settlement"):
        raise MarkPriceAdmissionError("funding first settlement mismatch")
    if rows[-1][0] != _utc(meta["last_settlement"], "last settlement"):
        raise MarkPriceAdmissionError("funding last settlement mismatch")
    return tuple(rows), meta


def admit_mark_price_source(
    *,
    symbol: str,
    funding_csv: Path,
    funding_manifest: Path,
    mark_csv: Path,
    mark_manifest: Path,
) -> dict:
    """Match every retained funding settlement to one complete 1m mark candle."""
    funding, funding_meta = _load_funding(funding_csv, funding_manifest, symbol)
    meta = json.loads(mark_manifest.read_text(encoding="utf-8"))
    mark_sha = _sha(mark_csv)
    if (
        meta.get("schema_version") != SCHEMA
        or meta.get("symbol") != symbol
        or meta.get("category") != "linear"
        or meta.get("interval_minutes") != 1
        or meta.get("endpoint") != OFFICIAL_ENDPOINT
        or meta.get("dataset_sha256") != mark_sha
        or meta.get("research_only") is not True
        or meta.get("strategy_promotion_allowed") is not False
    ):
        raise MarkPriceAdmissionError("mark-price manifest/source contract mismatch")
    with mark_csv.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != [
            "timestamp", "symbol", "open", "high", "low", "close"
        ]:
            raise MarkPriceAdmissionError("unexpected mark-price CSV schema")
        marks = {}
        previous = None
        for row in reader:
            if row["symbol"] != symbol:
                raise MarkPriceAdmissionError("mark-price symbol contamination")
            stamp = _utc(row["timestamp"], "mark timestamp")
            if (
                stamp.second != 0
                or stamp.microsecond != 0
                or stamp.minute < 0
                or stamp.minute > 59
            ):
                raise MarkPriceAdmissionError("mark candle must start on exact UTC minute")
            if previous is not None and stamp <= previous:
                raise MarkPriceAdmissionError("mark timestamps duplicated or reordered")
            previous = stamp
            values = tuple(
                _decimal(row[key], f"mark {key}")
                for key in ("open", "high", "low", "close")
            )
            open_, high, low, close = values
            if (
                min(values) <= 0
                or high < max(open_, low, close)
                or low > min(open_, high, close)
            ):
                raise MarkPriceAdmissionError("invalid mark-price OHLC")
            marks[stamp] = values
    funding_times = {stamp for stamp, _ in funding}
    mark_times = set(marks)
    if mark_times != funding_times:
        missing = sorted(funding_times - mark_times)
        extra = sorted(mark_times - funding_times)
        raise MarkPriceAdmissionError(
            f"mark/funding timestamp identity mismatch: missing={len(missing)} extra={len(extra)}"
        )
    rows = []
    total_width = ZERO
    maximum_width_fraction = ZERO
    for stamp, rate in funding:
        open_, high, low, close = marks[stamp]
        if high <= 0:
            raise MarkPriceAdmissionError("nonpositive mark high")
        width_fraction = (high - low) / ((high + low) / D("2"))
        total_width += width_fraction
        maximum_width_fraction = max(maximum_width_fraction, width_fraction)
        rows.append({
            "timestamp": stamp.isoformat(),
            "funding_rate": str(rate),
            "mark_open": str(open_),
            "mark_high": str(high),
            "mark_low": str(low),
            "mark_close": str(close),
            "mark_price_exact_at_settlement_verified": False,
            "one_minute_envelope_only": True,
        })
    return {
        "schema_version": "astra-mark-price-funding-admission-v1",
        "evaluation_class": "PUBLIC_MARK_PRICE_1M_ENVELOPE_RESEARCH_ONLY",
        "symbol": symbol,
        "settlements": len(rows),
        "first_settlement": rows[0]["timestamp"],
        "last_settlement": rows[-1]["timestamp"],
        "funding_source_sha256": _sha(funding_csv),
        "funding_manifest_sha256": _sha(funding_manifest),
        "mark_source_sha256": mark_sha,
        "mark_manifest_sha256": _sha(mark_manifest),
        "funding_endpoint_claim": funding_meta.get("endpoint"),
        "mark_endpoint_claim": meta.get("endpoint"),
        "all_funding_timestamps_have_exact_minute_mark_candle": True,
        "mark_price_exact_at_settlement_verified": False,
        "account_funding_ledger_verified": False,
        "provider_payload_signature_verified": False,
        "strategy_replay_completed": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "profitability_proven": False,
        "mean_one_minute_mark_range_fraction": str(total_width / D(len(rows))),
        "maximum_one_minute_mark_range_fraction": str(maximum_width_fraction),
        "rows": rows,
        "limitations": [
            "Bybit funding for USDT perpetuals uses quantity times mark price times funding rate",
            "Public 1m mark-price OHLC is an envelope, not the exact instantaneous settlement mark",
            "Public source files are not account-authoritative funding ledger records",
            "Opening/closing within about five seconds of funding may have uncertain participation",
            "No historical strategy profitability conclusion follows from source admission",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--funding-csv", type=Path, required=True)
    parser.add_argument("--funding-manifest", type=Path, required=True)
    parser.add_argument("--mark-csv", type=Path, required=True)
    parser.add_argument("--mark-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = admit_mark_price_source(
        symbol=args.symbol,
        funding_csv=args.funding_csv,
        funding_manifest=args.funding_manifest,
        mark_csv=args.mark_csv,
        mark_manifest=args.mark_manifest,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("MARK_PRICE_FUNDING_SOURCE_ADMITTED", json.dumps({
        "symbol": report["symbol"],
        "settlements": report["settlements"],
        "exact_mark_verified": False,
        "profitability_proven": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
