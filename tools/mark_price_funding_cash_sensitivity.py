"""Replay six-major funding cash with hourly Mark Price uncertainty envelopes.

This is a sensitivity layer on top of PR #324's exact Strategy A + hourly exit
replay. It changes only the funding position-value input from entry execution
price to a source-qualified Mark Price low/high interval for each settlement
hour. It does not alter signals, exits, risk, fees, slippage, or funding rates.

60m Mark Price is intentionally coarse and is NOT final venue-exact evidence.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.cross_sectional_portfolio import (
    PortfolioExitReason,
    ResearchDailyFundingContext,
    ResearchFundingCashflow,
)
from tools.daily_funding_cash_replay import (
    CASH,
    COSTS,
    INTRABAR,
    DailyFundingBook,
    load_signed_funding,
    make_result,
    replay,
    utc,
)
from tools.historical_ambiguity_census import BROAD_21, SIX_MAJOR
from tools.historical_portfolio_evidence import _common_timestamps
from tools.portfolio_hourly_replay import (
    SOURCE_ROOTS,
    FrozenSelection,
    HourlyCollisionResolver,
    digest,
    load_hourly_cases,
)

D = Decimal
ZERO = D("0")
MARK_SCENARIOS = ("MARK_COST_UPPER", "MARK_COST_LOWER")
MARK_SCHEMA = "bybit-mark-price-hourly-sensitivity-v1"
MARK_ENDPOINTS = {
    "https://api.bybit.com/v5/market/mark-price-kline",
    "https://api.bytick.com/v5/market/mark-price-kline",
}


class MarkFundingSensitivityError(ValueError):
    pass


def _sha(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise MarkFundingSensitivityError(f"source missing or symlinked: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_mark_envelopes(
    root: Path,
    *,
    symbols: tuple[str, ...],
    schedules,
    start,
    end,
):
    """Admit coarse Mark Price settlement rows and prove rate identity."""
    envelopes = {}
    fingerprints = {}
    for symbol in symbols:
        directory = root / symbol
        manifest_path = directory / f"{symbol}_mark_60m.manifest.json"
        raw_path = directory / f"{symbol}_mark_60m.csv"
        settlement_path = directory / f"{symbol}_funding_mark_60m.csv"
        if not manifest_path.is_file():
            raise MarkFundingSensitivityError(f"missing mark manifest: {symbol}")
        meta = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            meta.get("schema_version") != MARK_SCHEMA
            or meta.get("symbol") != symbol
            or meta.get("category") != "linear"
            or meta.get("endpoint") not in MARK_ENDPOINTS
            or meta.get("interval_minutes") != 60
            or meta.get("raw_mark_sha256") != _sha(raw_path)
            or meta.get("settlement_mark_sha256") != _sha(settlement_path)
            or meta.get("exact_settlement_mark_verified") is not False
            or meta.get("strategy_promotion_allowed") is not False
            or meta.get("profitability_proven") is not False
        ):
            raise MarkFundingSensitivityError(
                f"Mark Price source contract mismatch: {symbol}"
            )
        expected = {
            event.timestamp: event.rate
            for event in schedules[symbol]
            if start <= event.timestamp <= end + timedelta(days=1)
        }
        observed = {}
        with settlement_path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != [
                "timestamp",
                "symbol",
                "funding_rate",
                "mark_open",
                "mark_high",
                "mark_low",
                "mark_close",
                "hour_range_fraction",
            ]:
                raise MarkFundingSensitivityError(
                    "unexpected funding/Mark Price settlement schema"
                )
            previous = None
            for row in reader:
                stamp = utc(row["timestamp"])
                if row["symbol"] != symbol:
                    raise MarkFundingSensitivityError(
                        "Mark Price settlement symbol contamination"
                    )
                if previous is not None and stamp <= previous:
                    raise MarkFundingSensitivityError(
                        "Mark Price settlement timestamps reordered"
                    )
                previous = stamp
                rate = D(row["funding_rate"])
                low = D(row["mark_low"])
                high = D(row["mark_high"])
                open_ = D(row["mark_open"])
                close = D(row["mark_close"])
                width = D(row["hour_range_fraction"])
                values = (rate, low, high, open_, close, width)
                if not all(value.is_finite() for value in values):
                    raise MarkFundingSensitivityError("nonfinite Mark Price source")
                if (
                    min(low, high, open_, close) <= 0
                    or high < max(low, open_, close)
                    or low > min(high, open_, close)
                    or width < 0
                ):
                    raise MarkFundingSensitivityError("invalid Mark Price envelope")
                if stamp in observed:
                    raise MarkFundingSensitivityError("duplicate Mark Price settlement")
                observed[stamp] = (rate, low, high)
        if set(observed) != set(expected):
            raise MarkFundingSensitivityError(
                f"funding/Mark settlement identity differs: {symbol}"
            )
        for stamp, expected_rate in expected.items():
            if observed[stamp][0] != expected_rate:
                raise MarkFundingSensitivityError(
                    f"funding rate differs from retained source: {symbol} {stamp}"
                )
            _, low, high = observed[stamp]
            envelopes[(symbol, stamp)] = (low, high)
        fingerprints[f"{symbol}/manifest"] = _sha(manifest_path)
        fingerprints[f"{symbol}/raw"] = _sha(raw_path)
        fingerprints[f"{symbol}/settlement"] = _sha(settlement_path)
    return MappingProxyType(envelopes), dict(sorted(fingerprints.items()))


class MarkPriceFundingBook:
    """Same daily participation convention as #324, Mark notional substituted.

    MARK_COST_UPPER takes the higher possible cost of each Mark Price envelope.
    MARK_COST_LOWER takes the lower possible cost. For ambiguous participation,
    upper cost includes possible payments but excludes possible credits; lower
    cost does the inverse. Because cash feedback changes later size, resulting
    portfolio returns are sensitivity endpoints, not guaranteed global bounds.
    """

    def __init__(self, schedules, mark_envelopes, scenario: str):
        if scenario not in MARK_SCENARIOS:
            raise MarkFundingSensitivityError("unknown Mark Price scenario")
        indexed = {}
        for symbol, events in schedules.items():
            previous = None
            for event in events:
                if previous is not None and event.timestamp <= previous:
                    raise MarkFundingSensitivityError("funding events reordered")
                previous = event.timestamp
                day = event.timestamp.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                indexed.setdefault((symbol, day), []).append(event)
        self.events = MappingProxyType(
            {key: tuple(value) for key, value in indexed.items()}
        )
        self.symbols = frozenset(schedules)
        self.marks = mark_envelopes
        self.scenario = scenario
        self.audit: list[dict] = []
        self.daily_audit: list[dict] = []
        self.last_day = None

    def __call__(self, ctx: ResearchDailyFundingContext):
        day = ctx.day_utc
        if (
            day.tzinfo is None
            or day.utcoffset() != timedelta(0)
            or (day.hour, day.minute, day.second, day.microsecond) != (0, 0, 0, 0)
        ):
            raise MarkFundingSensitivityError("funding day must be UTC midnight")
        if self.last_day is not None and day != self.last_day + timedelta(days=1):
            raise MarkFundingSensitivityError("funding day skipped or repeated")
        exposures = []
        for trade in ctx.closed_today:
            if trade.exit_time != day or trade.entry_time > day:
                raise MarkFundingSensitivityError("closed trade belongs to wrong day")
            state = (
                "INTRABAR_EXIT"
                if trade.exit_reason in INTRABAR
                else "OPEN_EXIT"
            )
            exposures.append(
                (trade.symbol, trade.entry_time, trade.quantity, state)
            )
        for position in ctx.open_positions:
            if position.entry_time > day:
                raise MarkFundingSensitivityError("future open position")
            exposures.append(
                (
                    position.symbol,
                    position.entry_time,
                    position.quantity,
                    "OPEN_AT_CLOSE",
                )
            )
        names = [item[0] for item in exposures]
        if len(names) != len(set(names)):
            raise MarkFundingSensitivityError(
                "multiple same-symbol positions are not qualified"
            )

        flows = []
        pending = []
        for symbol, entered, quantity, state in sorted(exposures):
            if symbol not in self.symbols or quantity <= 0 or not quantity.is_finite():
                raise MarkFundingSensitivityError("invalid funding exposure")
            events = self.events.get((symbol, day))
            if not events:
                raise MarkFundingSensitivityError(
                    f"missing funding day for held symbol: {symbol} {day}"
                )
            for event in events:
                if state == "OPEN_EXIT" and event.timestamp != day:
                    continue
                key = (symbol, event.timestamp)
                if key not in self.marks:
                    raise MarkFundingSensitivityError(
                        f"missing Mark Price settlement envelope: {key}"
                    )
                mark_low, mark_high = self.marks[key]
                raw_a = quantity * mark_low * event.rate
                raw_b = quantity * mark_high * event.rate
                cost_min = min(raw_a, raw_b)
                cost_max = max(raw_a, raw_b)
                uncertain = state != "OPEN_AT_CLOSE" or event.timestamp == entered
                if self.scenario == "MARK_COST_UPPER":
                    chosen_cost = (
                        max(cost_max, ZERO) if uncertain else cost_max
                    )
                else:
                    chosen_cost = (
                        min(cost_min, ZERO) if uncertain else cost_min
                    )
                amount = -chosen_cost
                identity = (
                    f"research-mark-funding:{symbol}:"
                    f"{event.timestamp.isoformat()}:{self.scenario}"
                )
                pending.append(
                    {
                        "activity_id": identity,
                        "symbol": symbol,
                        "settlement_time": event.timestamp.isoformat(),
                        "booked_after_day_utc": (
                            day + timedelta(days=1)
                        ).isoformat(),
                        "entry_time": entered.isoformat(),
                        "holding_state": state,
                        "quantity": str(quantity),
                        "provider_rate": str(event.rate),
                        "participation_uncertain": uncertain,
                        "mark_low": str(mark_low),
                        "mark_high": str(mark_high),
                        "cost_min_usd": str(cost_min),
                        "cost_max_usd": str(cost_max),
                        "cash_amount_usd": str(amount),
                        "mark_price_exact_at_settlement_verified": False,
                        "mark_source_interval_minutes": 60,
                    }
                )
                if amount != ZERO:
                    flows.append(
                        ResearchFundingCashflow(
                            identity,
                            symbol,
                            event.timestamp,
                            amount,
                        )
                    )
        self.audit.extend(pending)
        self.daily_audit.append(
            {
                "day_utc": day.isoformat(),
                "net_funding_cash_usd": str(
                    sum((flow.amount for flow in flows), ZERO)
                ),
                "nonzero_flow_count": len(flows),
                "uncertain_event_count": sum(
                    row["participation_uncertain"] for row in pending
                ),
            }
        )
        self.last_day = day
        return tuple(flows)


def run_six_matched_sensitivity(snapshot_root: Path, mark_root: Path) -> dict:
    bars_dir = snapshot_root / "bybit_3y_2023-10-09_2026-10-08"
    funding_dir = snapshot_root / "bybit_funding_2023-10-09_2026-10-08"
    hourly = load_hourly_cases(
        bars_dir,
        tuple(snapshot_root / name for name in SOURCE_ROOTS),
    )
    all_loaded, common_dates = _common_timestamps(
        [bars_dir / f"{symbol}_D.csv" for symbol in BROAD_21]
    )
    funding_ends = {
        symbol: utc(
            json.loads(
                (
                    funding_dir / f"{symbol}_funding.manifest.json"
                ).read_text(encoding="utf-8")
            )["last_settlement"]
        )
        for symbol in BROAD_21
    }
    last_funding = min(funding_ends.values())
    dates = tuple(
        day
        for day in common_dates
        if day + timedelta(days=1) <= last_funding
    )
    if not dates or dates[-1].date().isoformat() != "2026-06-14":
        raise MarkFundingSensitivityError("matched chronology drifted")
    wanted = set(dates)
    sync = {
        symbol: tuple(
            bar for bar in all_loaded[symbol] if bar.timestamp in wanted
        )
        for symbol in SIX_MAJOR
    }
    bars = [bar for symbol in SIX_MAJOR for bar in sync[symbol]]
    schedules, funding_hashes = load_signed_funding(
        funding_dir,
        SIX_MAJOR,
        dates[0],
        dates[-1],
    )
    marks, mark_hashes = load_mark_envelopes(
        mark_root,
        symbols=SIX_MAJOR,
        schedules=schedules,
        start=dates[0],
        end=dates[-1],
    )
    original = replay(bars, *COSTS[0])
    selector = FrozenSelection(original.decision_trace, SIX_MAJOR)
    if replay(bars, *COSTS[0], selector=selector) != original:
        raise MarkFundingSensitivityError("Strategy A selector parity drifted")

    comparisons = []
    for fees, slip in COSTS:
        resolver = HourlyCollisionResolver(hourly)
        entry_upper = DailyFundingBook(schedules, "UPPER_COST")
        entry_result = replay(
            bars, fees, slip, selector, resolver, entry_upper
        )
        entry_record = make_result(
            entry_result, sync, fees, slip, entry_upper, resolver
        )
        row = {
            "fee_bps_per_fill": str(fees),
            "slippage_bps_per_side": str(slip),
            "entry_notional_upper_cost": entry_record,
            "mark_models": {},
        }
        for scenario in MARK_SCENARIOS:
            mark_book = MarkPriceFundingBook(schedules, marks, scenario)
            mark_resolver = HourlyCollisionResolver(hourly)
            result = replay(
                bars,
                fees,
                slip,
                selector,
                mark_resolver,
                mark_book,
            )
            record = make_result(
                result,
                sync,
                fees,
                slip,
                mark_book,
                mark_resolver,
            )
            row["mark_models"][scenario] = record
        comparisons.append(row)

    payload = {
        "schema_version": "astra-six-major-mark-price-funding-sensitivity-v1",
        "evaluation_class": (
            "REUSED_HISTORY_COARSE_60M_MARK_PRICE_CASH_FEEDBACK_SENSITIVITY"
        ),
        "research_only": True,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "untouched_holdout": False,
        "mark_source_interval_minutes": 60,
        "exact_settlement_mark_verified": False,
        "account_funding_ledger_verified": False,
        "same_strategy_and_exit_policy_as_pr324": True,
        "first_source_day_utc": dates[0].isoformat(),
        "last_source_day_utc": dates[-1].isoformat(),
        "source_funding_sha256": dict(sorted(funding_hashes.items())),
        "source_mark_price_sha256": mark_hashes,
        "comparisons": comparisons,
        "limitations": [
            "60m Mark Price low/high is deliberately coarse",
            "Daily cash booking timing remains the PR #324 research convention",
            "Funding participation at entry/exit boundaries remains uncertain",
            "Scenario portfolio returns are not guaranteed global mathematical bounds",
            "No 1m settlement admission or account-authoritative ledger yet",
        ],
    }
    serializable = json.loads(json.dumps(payload, default=str))
    serializable["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(serializable)
    ).hexdigest()
    return serializable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=Path("research/snapshots"),
    )
    parser.add_argument("--mark-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_six_matched_sensitivity(
        args.snapshot_root,
        args.mark_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for row in report["comparisons"]:
        print(
            "MARK_FUNDING_SENSITIVITY",
            json.dumps(
                {
                    "fees": row["fee_bps_per_fill"],
                    "slippage": row["slippage_bps_per_side"],
                    "entry_upper_return": row[
                        "entry_notional_upper_cost"
                    ]["metrics"]["return_including_daily_funding"],
                    "mark_upper_return": row["mark_models"][
                        "MARK_COST_UPPER"
                    ]["metrics"]["return_including_daily_funding"],
                    "mark_lower_return": row["mark_models"][
                        "MARK_COST_LOWER"
                    ]["metrics"]["return_including_daily_funding"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
    print("MARK_FUNDING_EVIDENCE", report["evidence_sha256"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
