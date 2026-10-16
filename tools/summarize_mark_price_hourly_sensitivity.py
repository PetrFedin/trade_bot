"""Aggregate and independently re-hash hourly Mark Price sensitivity artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tools.verify_mark_price_hourly_archive import verify_symbol_archive

D = Decimal
SIX = ("BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
SCHEMA = "bybit-mark-price-hourly-sensitivity-v1"


class HourlyMarkSummaryError(ValueError):
    pass


def _sha(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise HourlyMarkSummaryError(f"missing source file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decimal(value, label: str) -> Decimal:
    try:
        parsed = D(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise HourlyMarkSummaryError(f"invalid {label}") from exc
    if not parsed.is_finite():
        raise HourlyMarkSummaryError(f"{label} must be finite")
    return parsed


def summarize(root: Path, funding_root: Path | None = None) -> dict:
    manifests = []
    for symbol in SIX:
        directory = root / symbol
        path = directory / f"{symbol}_mark_60m.manifest.json"
        if not path.is_file():
            raise HourlyMarkSummaryError(f"manifest missing: {symbol}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            data.get("schema_version") != SCHEMA
            or data.get("symbol") != symbol
            or data.get("category") != "linear"
            or data.get("interval_minutes") != 60
            or data.get("exact_settlement_mark_verified") is not False
            or data.get("compatible_with_final_1m_admission") is not False
            or data.get("strategy_promotion_allowed") is not False
            or data.get("profitability_proven") is not False
        ):
            raise HourlyMarkSummaryError(f"manifest contract mismatch: {symbol}")
        raw = directory / f"{symbol}_mark_60m.csv"
        settlement = directory / f"{symbol}_funding_mark_60m.csv"
        if _sha(raw) != data.get("raw_mark_sha256"):
            raise HourlyMarkSummaryError(f"raw Mark Price hash mismatch: {symbol}")
        if _sha(settlement) != data.get("settlement_mark_sha256"):
            raise HourlyMarkSummaryError(f"settlement Mark Price hash mismatch: {symbol}")
        if data.get("hourly_rows", 0) <= 0 or data.get("funding_settlements", 0) <= 0:
            raise HourlyMarkSummaryError(f"empty source coverage: {symbol}")
        pages = data.get("provider_query_pages")
        if not isinstance(pages, list) or not pages:
            raise HourlyMarkSummaryError(f"missing provider page evidence: {symbol}")
        if any(
            not isinstance(page.get("provider_result_sha256"), str)
            or len(page["provider_result_sha256"]) != 64
            for page in pages
        ):
            raise HourlyMarkSummaryError(f"provider page digest malformed: {symbol}")
        if funding_root is not None:
            try:
                verify_symbol_archive(
                    root=root, funding_root=funding_root,
                    symbol=symbol, mark_manifest=data,
                )
            except ValueError as exc:
                raise HourlyMarkSummaryError(
                    f"independent funding/mark audit failed: {symbol}: {exc}"
                ) from exc
        manifests.append(data)

    start = {item["start"] for item in manifests}
    end = {item["end"] for item in manifests}
    if len(start) != 1 or len(end) != 1:
        raise HourlyMarkSummaryError("six-major collection chronology differs")

    weighted_width = D("0")
    settlements = 0
    maximum = D("0")
    total_hours = 0
    total_pages = 0
    by_symbol = {}
    for item in manifests:
        count = int(item["funding_settlements"])
        mean = _decimal(
            item["mean_settlement_hour_range_fraction"],
            "mean settlement hour range",
        )
        current_max = _decimal(
            item["maximum_settlement_hour_range_fraction"],
            "maximum settlement hour range",
        )
        if mean < 0 or current_max < 0 or mean > current_max:
            raise HourlyMarkSummaryError("invalid hourly uncertainty range")
        weighted_width += mean * D(count)
        settlements += count
        maximum = max(maximum, current_max)
        total_hours += int(item["hourly_rows"])
        total_pages += len(item["provider_query_pages"])
        by_symbol[item["symbol"]] = {
            "hourly_rows": int(item["hourly_rows"]),
            "funding_settlements": count,
            "provider_pages": len(item["provider_query_pages"]),
            "mean_settlement_hour_range_fraction": str(mean),
            "maximum_settlement_hour_range_fraction": str(current_max),
            "raw_mark_sha256": item["raw_mark_sha256"],
            "settlement_mark_sha256": item["settlement_mark_sha256"],
        }
    if settlements <= 0:
        raise HourlyMarkSummaryError("no funding settlements summarized")

    result = {
        "schema_version": "astra-six-major-mark-price-hourly-sensitivity-v1",
        "evaluation_class": "COARSE_PUBLIC_MARK_PRICE_ENVELOPE_RESEARCH_ONLY",
        "symbols": list(SIX),
        "start": next(iter(start)),
        "end": next(iter(end)),
        "hourly_rows_total": total_hours,
        "funding_settlements_total": settlements,
        "provider_pages_total": total_pages,
        "weighted_mean_settlement_hour_range_fraction": str(
            weighted_width / D(settlements)
        ),
        "maximum_settlement_hour_range_fraction": str(maximum),
        "by_symbol": dict(sorted(by_symbol.items())),
        "exact_settlement_mark_verified": False,
        "compatible_with_final_1m_admission": False,
        "account_funding_ledger_verified": False,
        "strategy_replay_completed": False,
        "strategy_promotion_allowed": False,
        "profitability_proven": False,
        "limitations": [
            "60m Mark Price is a coarse sensitivity envelope only",
            "Final source admission still requires settlement-aligned 1m Mark Price",
            "This summary does not use trade quantities or compute strategy PnL",
        ],
    }
    payload = json.dumps(
        result, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    result["evidence_sha256"] = hashlib.sha256(payload).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--funding-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = summarize(args.root, funding_root=args.funding_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("MARK_PRICE_HOURLY_SUMMARY", json.dumps({
        "hours": report["hourly_rows_total"],
        "settlements": report["funding_settlements_total"],
        "weighted_mean_hour_range_fraction": report[
            "weighted_mean_settlement_hour_range_fraction"
        ],
        "max_hour_range_fraction": report[
            "maximum_settlement_hour_range_fraction"
        ],
        "evidence_sha256": report["evidence_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
