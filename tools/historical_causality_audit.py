"""Bounded causality check for unchanged ASTRA Strategy A on reused Bybit archives.

Replay full and truncated history through the same selector, ledger, exit model
and optional funding. This detects some future-data leaks; it does not prove
absence of all lookahead, actual execution, or strategy profitability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.daily_funding_cash_replay import (
    COSTS,
    DailyFundingBook,
    load_signed_funding,
    replay,
    utc,
)
from tools.historical_ambiguity_census import BROAD_21, SIX_MAJOR
from tools.historical_execution_journal import verify_independent_receipt
from tools.historical_portfolio_evidence import _common_timestamps
from tools.portfolio_hourly_replay import (
    SOURCE_ROOTS,
    HourlyCollisionResolver,
    digest,
    load_hourly_cases,
)
from tools.publish_research_receipt import (
    ReceiptPublicationError,
    publish_new_json_receipt,
)

D = Decimal
PERIODS = ("six_full_history", "six_matched_window", "broad21_matched_window")
MODES = ("DAILY_STOP_FIRST:NO_FUNDING", "HOURLY_COLLISION_REFINED:UPPER_COST")
TOLERANCE = D("0.000000000001")


class CausalityError(ValueError):
    """Historical causality or immutable-source integrity was not established."""


def _day(value: str) -> datetime:
    result = utc(value)
    if (result.hour, result.minute, result.second, result.microsecond) != (0, 0, 0, 0):
        raise CausalityError("historical date is not midnight UTC")
    return result


def _plain_directory(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        raise CausalityError("required source directory must be plain")


def source_hash_gate(root: Path, report: dict) -> None:
    """Bind source to independently verified old 36-model report."""
    daily = root / "bybit_3y_2023-10-09_2026-10-08"
    funding = root / "bybit_funding_2023-10-09_2026-10-08"
    for path in (daily, funding, *(root / name for name in SOURCE_ROOTS)):
        _plain_directory(path)
    pins = report.get("source_daily_and_funding_sha256")
    if not isinstance(pins, dict) or not pins:
        raise CausalityError("source fingerprints absent")
    for key, checksum in sorted(pins.items()):
        if not isinstance(key, str) or not isinstance(checksum, str):
            raise CausalityError("source fingerprint malformed")
        parts = key.split("/", 1)
        if (
            len(parts) != 2 or parts[0] not in ("daily", "funding")
            or not parts[1] or "/" in parts[1] or "\\" in parts[1]
            or ".." in parts[1]
        ):
            raise CausalityError("unsafe source fingerprint identity")
        source = (daily if parts[0] == "daily" else funding) / parts[1]
        if not source.is_file() or source.is_symlink() or digest(source) != checksum:
            raise CausalityError(f"source bytes differ from money audit: {key}")


def _period_sources(root: Path):
    daily = root / "bybit_3y_2023-10-09_2026-10-08"
    funding = root / "bybit_funding_2023-10-09_2026-10-08"
    broad, common = _common_timestamps([
        daily / f"{s}_D.csv" for s in BROAD_21
    ])
    six_loaded, six_dates = _common_timestamps([
        daily / f"{s}_D.csv" for s in SIX_MAJOR
    ])
    end_dates = []
    for symbol in BROAD_21:
        file = funding / f"{symbol}_funding.manifest.json"
        if not file.is_file() or file.is_symlink():
            raise CausalityError("funding source manifest missing")
        meta = json.loads(file.read_text(encoding="utf-8"))
        if meta.get("symbol") != symbol:
            raise CausalityError("funding source identity differs")
        end_dates.append(utc(meta["last_settlement"]))
    funding_end = min(end_dates)
    matched = tuple(t for t in common if t + timedelta(days=1) <= funding_end)
    periods = (
        ("six_full_history", SIX_MAJOR, six_loaded, six_dates),
        ("six_matched_window", SIX_MAJOR, broad, matched),
        ("broad21_matched_window", BROAD_21, broad, matched),
    )
    for name, symbols, loaded, timeline in periods:
        if len(timeline) < 400 or len(set(timeline)) != len(timeline):
            raise CausalityError(f"{name}: insufficient or duplicate chronology")
        if any(b - a != timedelta(days=1) for a, b in zip(timeline, timeline[1:], strict=False)):
            raise CausalityError(f"{name}: missing UTC market day")
        accepted = set(timeline)
        for symbol in symbols:
            actual = tuple(b.timestamp for b in loaded[symbol] if b.timestamp in accepted)
            if actual != timeline:
                raise CausalityError(f"{name}: broken synchronized price history")
    return periods


def compare_prefix(full, prefix, *, full_book=None, prefix_book=None,
                   full_hourly=None, prefix_hourly=None) -> dict:
    """Compare every prior decision and execution fact, not merely final PnL."""
    if not prefix.decision_trace:
        raise CausalityError("prefix contains no trading decisions")
    cutoff = prefix.decision_trace[-1].execution_time
    expected = tuple(t for t in full.decision_trace if t.execution_time <= cutoff)
    if expected != prefix.decision_trace:
        raise CausalityError(f"future data changed a past decision: {cutoff}")
    past_trades = tuple(t for t in full.closed_trades if t.exit_time <= cutoff)
    if past_trades != prefix.closed_trades:
        raise CausalityError("future data changed an earlier closed trade")
    if prefix.ending_equity != prefix.decision_trace[-1].closing_equity:
        raise CausalityError("prefix terminal equity does not conserve")
    if expected[-1].closing_equity != prefix.ending_equity:
        raise CausalityError("future data changed historical marked equity")
    if (full_book is None) != (prefix_book is None):
        raise CausalityError("funding replay model differs")
    if (full_hourly is None) != (prefix_hourly is None):
        raise CausalityError("hourly replay model differs")
    funding_count = 0
    if full_book is not None:
        days = [r for r in full_book.daily_audit if _day(r["day_utc"]) <= cutoff]
        if days != prefix_book.daily_audit:
            raise CausalityError("funding daily cash path diverges")
        events = [
            r for r in full_book.audit
            if utc(r["settlement_time"]) < cutoff + timedelta(days=1)
        ]
        if events != prefix_book.audit:
            raise CausalityError("funding participation events diverge")
        funding_count = len(events)
    collisions = 0
    if full_hourly is not None:
        hourly = [r for r in full_hourly.audit if _day(r["day_utc"]) <= cutoff]
        if hourly != prefix_hourly.audit:
            raise CausalityError("hourly protective/target order diverges")
        if any(r["status"] == "MISSING_SOURCE_STOP_FIRST" for r in hourly):
            raise CausalityError("missing genuine hourly collision evidence")
        collisions = len(hourly)
    return {
        "cutoff_utc": cutoff.isoformat(),
        "compared_decisions": len(expected),
        "compared_closed_trades": len(past_trades),
        "compared_funding_events": funding_count,
        "compared_hourly_collisions": collisions,
        "ending_equity_usd": str(prefix.ending_equity),
        "historical_prefix_invariant": True,
    }


def _baseline(report: dict, period: str, fee: Decimal, slip: Decimal, mode: str):
    rows = [
        r for r in report["comparisons"]
        if r["period_id"] == period
        and r["fee_bps_per_fill"] == str(fee)
        and r["slippage_bps_per_side"] == str(slip)
    ]
    if len(rows) != 1 or mode not in rows[0]["models"]:
        raise CausalityError("missing frozen independently audited research model")
    return rows[0], rows[0]["models"][mode]["metrics"]


def _execute(bars, *, fees, slip, mode, funding_rates, hourly_cases):
    book = (DailyFundingBook(funding_rates, "UPPER_COST")
            if mode.endswith(":UPPER_COST") else None)
    hourly = (HourlyCollisionResolver(hourly_cases, require_source=True)
              if mode.startswith("HOURLY_COLLISION_REFINED") else None)
    # No selection cache: genuine selector receives only bars before execution day.
    result = replay(
        bars, fees, slip, selector=None, resolver=hourly, funding_book=book
    )
    return result, book, hourly


def audit(*, snapshots: Path, baseline: dict, verifier: dict) -> dict:
    if (
        baseline.get("schema_version") != "astra-daily-funding-cash-feedback-v1"
        or baseline.get("untouched_holdout") is not False
        or baseline.get("live_trading_allowed") is not False
        or baseline.get("strategy_promotion_allowed") is not False
    ):
        raise CausalityError("unsafe or unsupported historical baseline")
    auditor_sha = verify_independent_receipt(baseline, verifier)
    source_hash_gate(snapshots, baseline)
    source_dir = snapshots / "bybit_3y_2023-10-09_2026-10-08"
    funding_dir = snapshots / "bybit_funding_2023-10-09_2026-10-08"
    cases = load_hourly_cases(source_dir, tuple(snapshots / x for x in SOURCE_ROOTS))
    rows, checks = [], 0
    for period, symbols, loaded, timeline in _period_sources(snapshots):
        valid = set(timeline)
        source_bars = [
            b for s in symbols for b in loaded[s] if b.timestamp in valid
        ]
        schedules, _ = load_signed_funding(
            funding_dir, symbols, timeline[0], timeline[-1]
        )
        cuts = (len(timeline) // 4, len(timeline) // 2, 3 * len(timeline) // 4)
        for fees, slip in COSTS:
            for mode in MODES:
                original, target = _baseline(baseline, period, fees, slip, mode)
                full, full_book, full_hourly = _execute(
                    source_bars, fees=fees, slip=slip, mode=mode,
                    funding_rates=schedules, hourly_cases=cases,
                )
                if (
                    original["first_source_day_utc"] != timeline[0].isoformat()
                    or original["last_source_day_utc"] != timeline[-1].isoformat()
                    or full.decision_trace[0].execution_time.isoformat()
                    != original["first_execution_day_utc"]
                    or abs(full.total_return - D(target["return_including_daily_funding"]))
                    > TOLERANCE
                    or abs(full.ending_equity - D(target["ending_equity_usd"]))
                    > TOLERANCE
                    or full.closed_trade_count != target["closed_trades"]
                    or full.fill_count != target["fills"]
                ):
                    raise CausalityError("uncached full replay differs from original audit")
                prefix_rows = []
                for n in cuts:
                    until = timeline[n - 1]
                    cropped = [b for b in source_bars if b.timestamp <= until]
                    shorter, sb, sh = _execute(
                        cropped, fees=fees, slip=slip, mode=mode,
                        funding_rates=schedules, hourly_cases=cases,
                    )
                    prefix_rows.append(compare_prefix(
                        full, shorter, full_book=full_book, prefix_book=sb,
                        full_hourly=full_hourly, prefix_hourly=sh,
                    ))
                    checks += 1
                rows.append({
                    "period": period,
                    "symbols": list(symbols),
                    "original_model": mode,
                    "fee_bps": str(fees),
                    "slippage_bps": str(slip),
                    "historical_total_return": str(full.total_return),
                    "historical_closed_trades": full.closed_trade_count,
                    "reproduced_independent_money_report": True,
                    "cutoffs": prefix_rows,
                })
    if len(rows) != 12 or checks != 36:
        raise CausalityError("incomplete predefined causal prefix matrix")
    output = {
        "schema_version": "astra-causal-prefix-research-v1",
        "evaluation_class": "REUSED_HISTORY_BOUNDED_PREFIX_TEST_NOT_HOLDOUT",
        "original_report_sha256": baseline["evidence_sha256"],
        "independent_auditor_sha256": auditor_sha,
        "strategy": "UNCHANGED_STRATEGY_A",
        "model_count": len(rows),
        "prefix_count": checks,
        "all_tested_prefixes_invariant": True,
        "lookahead_absence_proven": False,
        "historical_source_provider_authenticated": False,
        "settlement_mark_price_verified": False,
        "exchange_fills_verified": False,
        "untouched_holdout": False,
        "profitability_proven": False,
        "strategy_promotion_allowed": False,
        "demo_trading_allowed": False,
        "live_trading_allowed": False,
        "models": rows,
        "scope": (
            "Three predefined cutoffs on repeatedly inspected 2023-2026 source. "
            "Not exhaustive lookahead analysis, venue accounting or future performance."
        ),
    }
    output["audit_sha256"] = hashlib.sha256(canonical_json_bytes(output)).hexdigest()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-root", type=Path, default=Path("research/snapshots"))
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--independent-verifier", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.baseline_report, args.independent_verifier):
        if not path.is_file() or path.is_symlink():
            raise CausalityError("missing or symlinked independent historical proof")
    value = audit(
        snapshots=args.snapshot_root,
        baseline=json.loads(args.baseline_report.read_text(encoding="utf-8")),
        verifier=json.loads(args.independent_verifier.read_text(encoding="utf-8")),
    )
    try:
        publish_new_json_receipt(args.output, value)
    except ReceiptPublicationError as exc:
        raise CausalityError(f"immutable audit publication refused: {exc}") from exc
    for item in value["models"]:
        print("CAUSAL_PREFIX_RESULT", json.dumps({
            "period": item["period"], "mode": item["original_model"],
            "fees": item["fee_bps"], "slippage": item["slippage_bps"],
            "historical_total_return": item["historical_total_return"],
            "prefixes": item["cutoffs"],
        }, sort_keys=True), flush=True)
    print("CAUSAL_AUDIT_SHA256", value["audit_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
