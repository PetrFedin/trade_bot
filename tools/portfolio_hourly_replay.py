"""Daily-clock cash-path replay using verified hourly bars for reached collisions.

Not a full intraday strategy, observed exchange execution or untouched holdout.
Funding is a separate post-replay approximation; it does not affect position size.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from app.marketdata.ohlcv import OhlcvBar
from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    ResearchCollisionContext,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelection, CrossSectionalSelector
from app.strategy.ohlcv_exit import IntrabarExitReason, evaluate_long_intrabar_exit
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from tools.historical_ambiguity_census import BROAD_21, SIX_MAJOR
from tools.historical_portfolio_evidence import _common_timestamps
from tools.historical_portfolio_funding import (
    FundingOpenPosition,
    funding_bounds_for_trades,
    load_funding_schedule,
)
from tools.intraday_evidence_admission import validate_intraday_fixture

D = Decimal
CASH = D("10000")
COSTS = ((D("8"), D("5")), (D("10"), D("10")))
SOURCE_ROOTS = (
    "bybit_intraday_trade_cases_60m",
    "bybit_ambiguous_six_hourly_20261013",
    "bybit_ambiguous_broad_hourly_20261013",
    "bybit_hourly_portfolio_stress_20261015",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


@dataclass(frozen=True)
class HourlyCase:
    bars: tuple[OhlcvBar, ...]
    raw_daily: tuple[Decimal, ...]
    source_sha256: str
    provenance_sha256: str


def load_hourly_cases(bars_dir: Path, source_roots: tuple[Path, ...]) -> dict:
    """Admit bytes, calendar and raw Decimal OHLCV; not signed venue provenance."""
    cases, daily = {}, {}
    for root in source_roots:
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"missing or symlinked hourly archive: {root}")
        for folder in sorted(root.iterdir()):
            if not folder.is_dir() or folder.is_symlink():
                raise ValueError("hourly archive must contain plain case directories")
            manifest = folder / "provenance.json"
            if manifest.is_symlink():
                raise ValueError("source manifest symlink forbidden")
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            symbol, days = meta.get("symbol"), meta.get("days")
            if not isinstance(days, list) or len(days) != 1 or symbol not in BROAD_21:
                raise ValueError("invalid single-symbol single-day source manifest")
            day = days[0]
            if meta.get("category") != "linear" or meta.get("research_only") is not True:
                raise ValueError("source must identify linear research-only contracts")
            if meta.get("strategy_promotion_allowed") is not False:
                raise ValueError("source may not authorize promotion")
            if folder.name != f"{symbol}_{day}":
                raise ValueError("source directory identity mismatch")
            path = folder / f"{symbol}_60m.csv"
            if path.is_symlink():
                raise ValueError("source CSV symlink forbidden")
            admission = validate_intraday_fixture(
                folder, symbols=(symbol,), days=(day,),
                interval_minutes=60, maximum_missing_fraction=0,
            )
            if not admission["admitted_for_research_only"] or admission["test_fixture_only"]:
                raise ValueError("synthetic data cannot become provider evidence")
            raw = rows(path)
            if len(raw) != 24:
                raise ValueError("need exactly 24 hourly bars, no extra days")
            first = datetime.fromisoformat(day).replace(tzinfo=UTC)
            hours = []
            for index, row in enumerate(raw):
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if stamp != first + timedelta(hours=index):
                    raise ValueError("hourly gap or timestamp outside declared UTC day")
                # Integer volume is a legacy unused field, NOT depth/fill evidence.
                # Reconcile the unrounded raw Decimal quantity below.
                hour = OhlcvBar(
                    symbol=symbol, timestamp=stamp,
                    **{key: D(row[key]) for key in ("open", "high", "low", "close")},
                    volume=int(D(row["volume"])), trade_count=0,
                )
                hour.validate()
                hours.append(hour)
            if symbol not in daily:
                source = rows(bars_dir / f"{symbol}_D.csv")
                daily[symbol] = {r["timestamp"][:10]: r for r in source}
                if len(daily[symbol]) != len(source):
                    raise ValueError("duplicate daily source date")
            original = daily[symbol].get(day)
            if original is None or original["symbol"] != symbol:
                raise ValueError("matching original daily source absent")
            expected = tuple(D(original[k]) for k in ("open", "high", "low", "close", "volume"))
            actual = (
                hours[0].open, max(h.high for h in hours), min(h.low for h in hours),
                hours[-1].close, sum((D(r["volume"]) for r in raw), D("0")),
            )
            if expected != actual:
                raise ValueError("hourly source does not conserve exact raw daily OHLCV")
            if (symbol, first) in cases:
                raise ValueError("duplicate hourly source for one symbol/day")
            cases[symbol, first] = HourlyCase(
                tuple(hours), expected, digest(path), digest(manifest),
            )
    return cases


class HourlyCollisionResolver:
    def __init__(self, cases: dict, *, require_source: bool = False):
        self.cases = MappingProxyType(dict(cases))
        self.require_source = require_source
        self.audit: list[dict] = []

    def __call__(self, ctx: ResearchCollisionContext):
        original = ctx.daily_decision
        if not original.ambiguous_bar or not original.exit_now:
            raise ValueError("hourly resolver is restricted to reached daily collisions")
        case = self.cases.get((ctx.bar.symbol, ctx.bar.timestamp))
        row = {
            "symbol": ctx.bar.symbol, "day_utc": ctx.bar.timestamp.isoformat(),
            "actual_portfolio_average_cost": str(ctx.average_cost),
            "hard_stop": str(original.hard_stop_price), "target": str(original.take_profit_price),
            "daily_reason": original.reason.value,
            "daily_reference_price": str(original.exit_price_before_costs),
            "provider_fills_verified": False,
        }
        if case is None:
            row.update(status="MISSING_SOURCE_STOP_FIRST", changed=False)
            self.audit.append(row)
            if self.require_source:
                raise ValueError(f"new counterfactual collision has no hourly source: {row}")
            return original
        if case.raw_daily[:4] != (ctx.bar.open, ctx.bar.high, ctx.bar.low, ctx.bar.close):
            raise ValueError("hourly source differs from actual current portfolio daily bar")
        for hour in case.bars:
            # PRIOR-DAY state is unchanged across ALL hours. Updating the peak
            # here would silently convert original daily trailing to hourly trailing.
            refined = evaluate_long_intrabar_exit(
                average_cost=ctx.average_cost, bar=hour, state=ctx.state, policy=ctx.policy,
            )
            if not refined.exit_now:
                continue
            status = (
                "UNRESOLVED_SAME_HOUR_STOP_FIRST" if refined.ambiguous_bar
                else "TARGET_FIRST_HOURLY" if refined.reason == IntrabarExitReason.TAKE_PROFIT
                else "PROTECTIVE_FIRST_HOURLY"
            )
            row.update(
                status=status, first_touch_hour=hour.timestamp.isoformat(),
                refined_reason=refined.reason.value,
                refined_reference_price=str(refined.exit_price_before_costs),
                changed=(refined.reason, refined.exit_price_before_costs) != (
                    original.reason, original.exit_price_before_costs,
                ),
                source_sha256=case.source_sha256,
            )
            self.audit.append(row)
            return refined
        raise ValueError("daily collision must have an observed hourly threshold touch")


class FrozenSelection(CrossSectionalSelector):
    def __init__(self, trace, symbols):
        super().__init__(top_k=2)
        self.symbols = tuple(symbols)
        self.trace = {t.decision_time: t for t in trace}
        if len(self.trace) != len(trace):
            raise ValueError("duplicate frozen decision")

    def select(self, bars):
        if not bars or bars[-1].symbol != self.symbols[-1]:
            raise ValueError("wrong ordered prior history")
        original = self.trace.get(bars[-1].timestamp)
        if original is None or len(bars) != original.execution_index * len(self.symbols):
            raise ValueError("frozen selection context differs from source")
        return CrossSectionalSelection(original.decision_time, original.selected_symbols, ())


def replay(bars, *, fees, slip, selector=None, resolver=None):
    return CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=2) if selector is None else selector,
        portfolio_policy=CrossSectionalPortfolioPolicy(
            opening_cash=CASH, fee_per_fill=D("0"), fee_bps_per_fill=fees,
            slippage_bps=slip, maximum_gross_exposure_fraction=D("0.60"),
            new_position_target_equity_fraction=D("0.29"),
        ),
        reentry_policy=ReentryConfirmationPolicy(minimum_consecutive_eligible_bars=2),
    ).run(bars, research_collision_resolver=resolver)


def metrics(result, schedules, sync, timeline, slip, fee_bps):
    remaining = []
    open_cost, open_mark, open_fees = D("0"), D("0"), D("0")
    for symbol, qty in result.final_quantities.items():
        if qty <= 0:
            continue
        entry = next(t for t in reversed(result.decision_trace) if symbol in t.entered_symbols)
        raw = sync[symbol][entry.execution_index]
        execution_price = raw.open * (D("1") + slip / D("10000"))
        notional = qty * execution_price
        entry_fee = notional * fee_bps / D("10000")
        open_fees += entry_fee
        open_cost += notional + entry_fee
        open_mark += qty * sync[symbol][-1].close
        remaining.append(FundingOpenPosition(
            symbol, entry.execution_time, timeline[-1] + timedelta(days=1), qty,
            execution_price,
        ))
    funding = funding_bounds_for_trades(
        result.closed_trades, schedules, open_positions=tuple(remaining),
    )
    if abs(result.ending_equity - CASH - result.total_pnl) > D("0.000001"):
        raise ValueError("portfolio cash/equity identity failed")
    for t in result.closed_trades:
        expected = t.gross_pnl_before_costs - t.slippage_cost - t.entry_fee - t.exit_fee
        if abs(t.net_pnl - expected) > D("0.00000001"):
            raise ValueError("gross, slippage, fees and net trade do not conserve")
    closed_net = sum((t.net_pnl for t in result.closed_trades), D("0"))
    cash_from_trades = CASH + closed_net - open_cost
    fees_from_trades = sum(
        (t.entry_fee + t.exit_fee for t in result.closed_trades), open_fees,
    )
    if abs(cash_from_trades + open_mark - result.ending_equity) > D("0.000001"):
        raise ValueError("independent closed-trade plus open-inventory cash reconciliation failed")
    if abs(fees_from_trades - result.fees_paid) > D("0.000001"):
        raise ValueError("independent all-fill fee reconstruction failed")
    peak, peak_relative_dd = CASH, D("0")
    for t in result.decision_trace:
        peak = max(peak, t.closing_equity)
        peak_relative_dd = max(peak_relative_dd, (peak - t.closing_equity) / peak)
    return {
        "ending_equity_before_funding_usd": str(result.ending_equity),
        "ending_cash_before_funding_usd": str(cash_from_trades),
        "closed_net_pnl_before_funding_usd": str(closed_net),
        "final_open_cost_including_entry_fee_usd": str(open_cost),
        "final_open_marked_value_usd": str(open_mark),
        "all_fill_fees_independently_reconstructed_usd": str(fees_from_trades),
        "return_before_funding": str(result.total_return),
        "legacy_funding_cost_lower_usd": str(funding.lower_cost),
        "legacy_funding_cost_upper_usd": str(funding.upper_cost),
        "return_after_legacy_funding_lower": str((result.total_pnl - funding.upper_cost) / CASH),
        "return_after_legacy_funding_upper": str((result.total_pnl - funding.lower_cost) / CASH),
        "funding_cash_feedback_modelled": False,
        "closed_trades": result.closed_trade_count, "fill_count": result.fill_count,
        "fees_paid_usd": str(result.fees_paid),
        "turnover_over_starting_cash": str(result.turnover_fraction),
        "max_daily_close_drawdown_over_starting_cash": str(result.max_drawdown_fraction),
        "max_daily_close_drawdown_over_peak_equity": str(peak_relative_dd),
        "remaining_ambiguous_daily_exits": sum(
            t.ambiguous_intrabar_exit for t in result.closed_trades
        ),
        "final_open_positions": len(remaining),
    }


def run_study(snapshot_root: Path, *, universes=("six", "broad21")) -> dict:
    if not universes or len(set(universes)) != len(universes):
        raise ValueError("universe list must be nonempty and unique")
    bars_dir = snapshot_root / "bybit_3y_2023-10-09_2026-10-08"
    funding_dir = snapshot_root / "bybit_funding_2023-10-09_2026-10-08"
    sources = load_hourly_cases(bars_dir, tuple(snapshot_root / r for r in SOURCE_ROOTS))
    reports, source_digests = [], {}
    for universe in universes:
        if universe not in ("six", "broad21"):
            raise ValueError("unknown fixed universe")
        symbols = SIX_MAJOR if universe == "six" else BROAD_21
        paths = [bars_dir / f"{s}_D.csv" for s in symbols]
        loaded, timeline = _common_timestamps(paths)
        wanted = set(timeline)
        sync = {s: tuple(b for b in loaded[s] if b.timestamp in wanted) for s in symbols}
        bars = [b for s in symbols for b in sync[s]]
        schedules = {}
        for s, path in zip(symbols, paths, strict=True):
            fp = funding_dir / f"{s}_funding.csv"
            fm = funding_dir / f"{s}_funding.manifest.json"
            meta = json.loads(fm.read_text())
            if meta["dataset_sha256"] != digest(fp) or meta["symbol"] != s:
                raise ValueError("funding source checksum or symbol mismatch")
            schedules[s] = load_funding_schedule(fp)
            if not schedules[s] or any(
                b.timestamp <= a.timestamp
                for a, b in zip(schedules[s], schedules[s][1:], strict=False)
            ):
                raise ValueError("missing or duplicate funding settlements")
            if any(not x.rate.is_finite() for x in schedules[s]):
                raise ValueError("nonfinite funding source")
            for source in (path, fp, fm):
                source_digests[str(source.relative_to(snapshot_root))] = digest(source)
        print("START_CONTROL", universe, flush=True)
        original = replay(bars, fees=D("8"), slip=D("5"))
        if original.closed_trade_count != (256 if universe == "six" else 276):
            raise ValueError("original Strategy A trade count drifted")
        expected = (D("0.007515525902184908204853186") if universe == "six"
                    else D("-0.1797085857706086935251021902"))
        if abs(original.total_return - expected) > D("1e-12"):
            raise ValueError("original Strategy A equity drifted")
        selector = FrozenSelection(original.decision_trace, symbols)
        if replay(bars, fees=D("8"), slip=D("5"), selector=selector) != original:
            raise ValueError("cached baseline differs from full uncached control")
        for fees, slip in COSTS:
            control = original if (fees, slip) == COSTS[0] else replay(
                bars, fees=fees, slip=slip, selector=selector,
            )
            resolver = HourlyCollisionResolver(sources)
            refined = replay(bars, fees=fees, slip=slip, selector=selector, resolver=resolver)
            base = metrics(control, schedules, sync, timeline, slip, fees)
            alt = metrics(refined, schedules, sync, timeline, slip, fees)
            counts = dict(sorted(Counter(a["status"] for a in resolver.audit).items()))
            old_map = {(t.symbol, t.entry_time, t.exit_time): t for t in control.closed_trades}
            quantity_changes = sum(
                abs(t.quantity - old_map[(t.symbol, t.entry_time, t.exit_time)].quantity)
                > D("1e-15")
                for t in refined.closed_trades
                if (t.symbol, t.entry_time, t.exit_time) in old_map
            )
            row = {
                "universe": universe, "symbols": list(symbols),
                "source_start": timeline[0].isoformat(), "source_end": timeline[-1].isoformat(),
                "fee_bps_per_fill": str(fees), "slippage_bps_per_side": str(slip),
                "control": base, "hourly_refined": alt,
                "delta_return_before_funding": str(refined.total_return - control.total_return),
                "delta_return_after_legacy_funding_lower": str(
                    D(alt["return_after_legacy_funding_lower"])
                    - D(base["return_after_legacy_funding_lower"])
                ),
                "reached_collision_counts": counts,
                "all_reached_collisions_sourced": "MISSING_SOURCE_STOP_FIRST" not in counts,
                "changed_matched_trade_quantities": quantity_changes,
                "changed_daily_closing_equity_rows": sum(
                    a.closing_equity != b.closing_equity
                    for a, b in zip(control.decision_trace, refined.decision_trace, strict=True)
                ),
                "collision_audit": resolver.audit,
                "control_trades": [asdict(t) for t in control.closed_trades],
                "refined_trades": [asdict(t) for t in refined.closed_trades],
                "control_daily_equity": [str(t.closing_equity) for t in control.decision_trace],
                "refined_daily_equity": [str(t.closing_equity) for t in refined.decision_trace],
            }
            reports.append(row)
            print("PORTFOLIO_REPLAY", json.dumps({
                k: v for k, v in row.items() if k not in (
                    "control_trades", "refined_trades", "control_daily_equity",
                    "refined_daily_equity", "collision_audit", "symbols",
                )
            }), flush=True)
    payload = {
        "schema_version": "astra-hourly-refined-daily-portfolio-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_FULL_CASH_PATH_SENSITIVITY",
        "research_only": True, "strategy_promotion_allowed": False,
        "live_trading_allowed": False, "untouched_holdout": False,
        "funding_cash_feedback_modelled": False,
        "intraday_reinvestment_enabled": False, "trailing_timeframe_changed": False,
        "provider_fills_verified": False, "source_provenance_independently_signed": False,
        "source_daily_and_funding_sha256": source_digests,
        "hourly_cases_in_archive": len(sources),
        "hourly_source_sha256": {
            f"{s}_{t.date()}": {"csv": v.source_sha256, "provenance": v.provenance_sha256}
            for (s, t), v in sorted(sources.items())
        },
        "comparisons": reports,
        "limitations": [
            "Only reached ambiguous daily exits use hourly bars; not a full intraday strategy",
            "Missing newly reached source stays stop-first and is explicitly reported",
            "Hourly touches are not guaranteed fills; same-hour ambiguity remains stop-first",
            "Signals and trailing use prior daily bars; no intraday reinvestment",
            "Funding is post-replay, not settled into cash before subsequent position sizing",
            "Legacy funding clips net credits per trade; ranges are not statistical bounds",
            "Constant entry notional differs from venue mark-price funding",
            "Daily-close drawdown omits intraday losses; fixed spreads/fees are assumptions",
            "Fee-capitalized cost is inherited from the original model, not venue trigger price",
            "Six/broad21 history end dates differ; not a matched-universe comparison",
            "Old inspected samples and previous experiments do not constitute fresh OOS",
        ],
    }
    serializable = json.loads(json.dumps(payload, default=str))
    serializable["evidence_sha256"] = hashlib.sha256(canonical_json_bytes(serializable)).hexdigest()
    return serializable


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot-root", type=Path, default=Path("research/snapshots"))
    p.add_argument("--universe", choices=("six", "broad21"), action="append")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = run_study(args.snapshot_root, universes=tuple(args.universe or ("six", "broad21")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("FULL_CASH_REPLAY_EVIDENCE", result["evidence_sha256"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
