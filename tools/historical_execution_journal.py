"""Deterministic, read-only decision/trade journal for inspected ASTRA history.

Consume a separately verified 36-model PR324 replay; never create signals,
reinterpret exchange fills, select winning parameters, or grant trading authority.
Window equity returns are path-dependent; closed-trade PnL is attribution only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes

D = Decimal
ZERO = D("0")
CASH = D("10000")
TOLERANCE = D("0.000001")
PERIODS = ("six_full_history", "six_matched_window", "broad21_matched_window")
COSTS = (("8", "5"), ("10", "10"))
MODELS = tuple(
    f"{model}:{funding}"
    for model in ("DAILY_STOP_FIRST", "HOURLY_COLLISION_REFINED")
    for funding in ("NO_FUNDING", "UPPER_COST", "LOWER_COST")
)
EXIT_REASONS = frozenset((
    "SELECTION_EXIT", "TIME_STOP", "INTRABAR_HARD_STOP",
    "INTRABAR_TAKE_PROFIT", "INTRABAR_TRAILING_STOP",
))


class JournalError(ValueError):
    """A scenario or timeline does not match admitted historical research."""


def number(value: object) -> Decimal:
    if isinstance(value, bool):
        raise JournalError("boolean is not monetary evidence")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise JournalError("invalid decimal input") from exc
    if not result.is_finite():
        raise JournalError("nonfinite decimal input")
    return result


def date(value: object) -> datetime:
    if not isinstance(value, str):
        raise JournalError("timestamp must be a UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JournalError("bad historical timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise JournalError("non-UTC timestamp")
    parsed = parsed.astimezone(UTC)
    if parsed.time().isoformat() != "00:00:00":
        raise JournalError("daily execution clock must be UTC midnight")
    return parsed


def near(left: object, right: object, *, label: str) -> None:
    if abs(number(left) - number(right)) > TOLERANCE:
        raise JournalError(f"{label} does not conserve")


def _trade_excerpt(row: dict) -> dict:
    return {
        "symbol": row["symbol"],
        "entry_utc": row["entry_time"],
        "exit_utc": row["exit_time"],
        "holding_bars": row["holding_bars"],
        "exit_reason": row["exit_reason"],
        "quantity": str(number(row["quantity"])),
        "entry_execution_price": str(number(row["entry_execution_price"])),
        "exit_execution_price": str(number(row["exit_execution_price"])),
        "entry_fee_usd": str(number(row["entry_fee"])),
        "exit_fee_usd": str(number(row["exit_fee"])),
        "slippage_cost_usd": str(number(row["slippage_cost"])),
        "gross_pnl_usd": str(number(row["gross_pnl_before_costs"])),
        "net_pnl_usd": str(number(row["net_pnl"])),
        "ambiguous_intrabar_exit": row["ambiguous_intrabar_exit"],
        "gap_through_stop": row["gap_through_stop"],
    }


def analyze_model(comparison: dict, key: str, *, window_days: int = 90) -> dict:
    """Build non-overlapping capital-path diagnostics for a single frozen model."""
    if key not in MODELS or window_days < 20:
        raise JournalError("unregistered research model or window")
    model = comparison["models"][key]
    metrics = model["metrics"]
    daily = model["daily_equity"]
    if not daily:
        raise JournalError("missing daily account equity")
    first = date(comparison["first_execution_day_utc"])
    final = date(comparison["last_source_day_utc"])
    prior = CASH
    for i, row in enumerate(daily):
        stamp = date(row["day_utc"])
        if stamp != first + timedelta(days=i):
            raise JournalError("duplicate, missing or out-of-order trading day")
        near(row["prior_equity"], prior, label="daily carried equity")
        closing = number(row["closing_equity"])
        if closing <= ZERO or prior <= ZERO:
            raise JournalError("nonpositive modelled equity")
        if (
            not isinstance(row["entered_symbols"], list)
            or not isinstance(row["selected_symbols"], list)
        ):
            raise JournalError("unrecorded decision trace schema")
        if len(set(row["entered_symbols"])) != len(row["entered_symbols"]):
            raise JournalError("duplicate daily entry")
        if (
            len(row["entered_symbols"]) > 2
            or not set(row["entered_symbols"]).issubset(row["selected_symbols"])
        ):
            raise JournalError("selected-to-entered decision linkage failed")
        prior = closing
    if date(daily[-1]["day_utc"]) != final:
        raise JournalError("last daily mark does not match source end")
    near(prior, metrics["ending_equity_usd"], label="final daily versus portfolio equity")
    near(
        (prior / CASH) - D("1"),
        metrics["return_including_daily_funding"],
        label="reported portfolio return",
    )
    seen = set()
    by_symbol = defaultdict(lambda: {"count": 0, "wins": 0, "net": ZERO})
    reasons = Counter()
    holdings = Counter()
    parsed_trades = []
    total_trade_net = ZERO
    total_trade_fees = ZERO
    total_slippage = ZERO
    for trade in model["trades"]:
        entered, exited = date(trade["entry_time"]), date(trade["exit_time"])
        symbol = trade["symbol"]
        identity = (symbol, entered)
        if (
            not isinstance(symbol, str) or symbol not in comparison["symbols"]
            or identity in seen or not first <= entered <= exited <= final
        ):
            raise JournalError("invalid trade identity, instrument or chronology")
        seen.add(identity)
        if trade["exit_reason"] not in EXIT_REASONS:
            raise JournalError("unsupported exit reason")
        holding = trade["holding_bars"]
        if type(holding) is not int or holding != (exited - entered).days:
            raise JournalError("trade holding days inconsistent")
        quantity, ep, xp = (
            number(trade["quantity"]), number(trade["entry_execution_price"]),
            number(trade["exit_execution_price"]),
        )
        entry_fee, exit_fee = number(trade["entry_fee"]), number(trade["exit_fee"])
        gross, slip, net = (
            number(trade["gross_pnl_before_costs"]),
            number(trade["slippage_cost"]), number(trade["net_pnl"]),
        )
        if (
            quantity <= ZERO or ep <= ZERO or xp <= ZERO
            or entry_fee < ZERO or exit_fee < ZERO or slip < ZERO
        ):
            raise JournalError("invalid trade price, size or cost")
        near(gross - slip - entry_fee - exit_fee, net, label="trade cost decomposition")
        near(quantity * (xp - ep) - entry_fee - exit_fee, net, label="executed-price trade PnL")
        if (
            type(trade["ambiguous_intrabar_exit"]) is not bool
            or type(trade["gap_through_stop"]) is not bool
        ):
            raise JournalError("invalid intrabar/gap trace")
        parsed_trades.append((exited, net, trade))
        total_trade_net += net
        total_trade_fees += entry_fee + exit_fee
        total_slippage += slip
        reasons[trade["exit_reason"]] += 1
        holdings["same_day" if holding == 0 else "next_day" if holding == 1 else "longer"] += 1
        by_symbol[symbol]["count"] += 1
        by_symbol[symbol]["net"] += net
        by_symbol[symbol]["wins"] += int(net > ZERO)
    if len(parsed_trades) != metrics["closed_trades"]:
        raise JournalError("closed-trade count does not match tape")
    near(total_trade_net, metrics["closed_trade_net_excluding_funding_usd"],
         label="trade net PnL total")
    if number(metrics["execution_fees_usd"]) + TOLERANCE < total_trade_fees:
        raise JournalError("reported all-fill fees lower than closed trades")

    funding_days = model["funding_days"]
    funding_by_day = {}
    if key.endswith("NO_FUNDING"):
        if funding_days or model["funding_events"]:
            raise JournalError("NO_FUNDING cannot contain funding records")
    else:
        if len(funding_days) != len(daily):
            raise JournalError("incomplete funding-day audit")
        for i, flow in enumerate(funding_days):
            stamp = date(flow["day_utc"])
            if stamp != date(daily[i]["day_utc"]) or stamp in funding_by_day:
                raise JournalError("funding-to-equity daily clock broken")
            funding_by_day[stamp] = number(flow["net_funding_cash_usd"])
    funding_total = sum(funding_by_day.values(), ZERO)
    near(funding_total, metrics["funding_cash_net_usd"], label="daily booked funding")
    event_total = sum((number(e["cash_amount_usd"]) for e in model["funding_events"]), ZERO)
    near(event_total, funding_total, label="funding events to day ledger")

    intervals = []
    for left in range(0, len(daily), window_days):
        block = daily[left:left + window_days]
        start_date = date(block[0]["day_utc"])
        end_date = date(block[-1]["day_utc"])
        opening = number(block[0]["prior_equity"])
        ending = number(block[-1]["closing_equity"])
        peak, drawdown = opening, ZERO
        for row in block:
            peak = max(peak, number(row["closing_equity"]))
            drawdown = max(drawdown, (peak - number(row["closing_equity"])) / peak)
        close_trades = [(pnl, t) for dt, pnl, t in parsed_trades if start_date <= dt <= end_date]
        intervals.append({
            "start_utc": start_date.date().isoformat(),
            "end_utc": end_date.date().isoformat(),
            "days": len(block),
            "complete_window": len(block) == window_days,
            "starting_equity_usd": str(opening),
            "ending_equity_usd": str(ending),
            "equity_return_pct": str(D("100") * (ending / opening - D("1"))),
            "max_daily_close_drawdown_pct": str(D("100") * drawdown),
            "closed_trade_count": len(close_trades),
            "wins": sum(pnl > ZERO for pnl, _ in close_trades),
            "closed_trade_net_pnl_usd_attributed": str(sum((pnl for pnl, _ in close_trades), ZERO)),
            "funding_cash_usd_booked": str(sum(
                (amount for stamp, amount in funding_by_day.items()
                 if start_date <= stamp <= end_date), ZERO
            )),
            "entries": sum(len(r["entered_symbols"]) for r in block),
        })
    examples = sorted(parsed_trades, key=lambda row: (row[1], row[0], row[2]["symbol"]))
    return {
        "model_key": key,
        "first_execution_day_utc": first.isoformat(),
        "last_source_day_utc": final.isoformat(),
        "trading_days": len(daily),
        "portfolio_return_pct": str(D("100") * number(metrics["return_including_daily_funding"])),
        "ending_equity_usd": str(number(metrics["ending_equity_usd"])),
        "max_drawdown_pct": str(D("100") * number(metrics["daily_close_max_drawdown_over_peak"])),
        "closed_trades": len(parsed_trades),
        "fills": metrics["fills"],
        "exit_reasons": dict(sorted(reasons.items())),
        "holding_days": {key: holdings[key] for key in ("same_day", "next_day", "longer")},
        "closed_trade_net_usd": str(total_trade_net),
        "closed_trade_fees_usd": str(total_trade_fees),
        "closed_trade_slippage_usd": str(total_slippage),
        "funding_cash_net_usd": str(funding_total),
        "by_symbol": {
            k: {"closed_trades": v["count"], "wins": v["wins"],
                "closed_trade_net_usd": str(v["net"])}
            for k, v in sorted(by_symbol.items())
        },
        "windows": intervals,
        "worst_closed_trades": [_trade_excerpt(t) for _, _, t in examples[:5]],
        "best_closed_trades": [_trade_excerpt(t) for _, _, t in reversed(examples[-5:])],
    }


def build_journal(report: dict) -> dict:
    """All 36 predeclared ledgers, with matched chronology and zero authority."""
    if (
        report.get("schema_version") != "astra-daily-funding-cash-feedback-v1"
        or report.get("evaluation_class")
        != "REUSED_HISTORY_DAILY_CASH_FEEDBACK_NOT_VENUE_SETTLEMENT"
        or report.get("research_only") is not True
    ):
        raise JournalError("unsupported historical source")
    for flag in (
        "strategy_promotion_allowed", "live_trading_allowed", "untouched_holdout",
        "funding_exact_exchange_timing", "mark_price_used", "margin_or_liquidation_modelled",
    ):
        if report.get(flag) is not False:
            raise JournalError("research scope upgraded without evidence")
    recorded = report.get("evidence_sha256")
    if not isinstance(recorded, str) or len(recorded) != 64:
        raise JournalError("missing source report digest")
    observed = hashlib.sha256(canonical_json_bytes(
        {k: v for k, v in report.items() if k != "evidence_sha256"}
    )).hexdigest()
    if recorded != observed:
        raise JournalError("source report hash mismatch")
    comparisons = report.get("comparisons")
    expected = {(period, *cost) for period in PERIODS for cost in COSTS}
    if not isinstance(comparisons, list) or len(comparisons) != len(expected):
        raise JournalError("incomplete 36-model comparison matrix")
    collected = {}
    for comparison in comparisons:
        key = (
            comparison["period_id"], comparison["fee_bps_per_fill"],
            comparison["slippage_bps_per_side"],
        )
        if key not in expected or key in collected or set(comparison["models"]) != set(MODELS):
            raise JournalError("unregistered or incomplete comparison matrix")
        if not isinstance(comparison["symbols"], list):
            raise JournalError("missing instrument universe")
        models = {m: analyze_model(comparison, m) for m in MODELS}
        collected[key] = {
            "period_id": key[0],
            "fee_bps_per_fill": key[1],
            "slippage_bps_per_side": key[2],
            "symbols": comparison["symbols"],
            "models": models,
        }
    matched_pairs = []
    for fee, slip in COSTS:
        six = collected[("six_matched_window", fee, slip)]
        broad = collected[("broad21_matched_window", fee, slip)]
        for model in MODELS:
            a, b = six["models"][model], broad["models"][model]
            if (
                a["first_execution_day_utc"] != b["first_execution_day_utc"]
                or a["last_source_day_utc"] != b["last_source_day_utc"]
                or a["trading_days"] != b["trading_days"]
            ):
                raise JournalError("matched universes do not share test dates")
            six_return = number(a["portfolio_return_pct"])
            broad_return = number(b["portfolio_return_pct"])
            matched_pairs.append({
                "fee_bps_per_fill": fee, "slippage_bps_per_side": slip,
                "model_key": model, "six_return_pct": str(six_return),
                "broad21_return_pct": str(broad_return),
                "six_minus_broad21_percentage_points": str(six_return - broad_return),
                "same_dates_verified": True,
            })
    result = {
        "schema_version": "astra-trade-journal-reused-history-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_DIAGNOSTIC_ONLY",
        "source_report_sha256": recorded,
        "window_size_days": 90,
        "scenario_count": len(collected),
        "model_count": len(collected) * len(MODELS),
        "comparisons": [collected[k] for k in sorted(collected)],
        "matched_universe_differences": matched_pairs,
        "modelled_trading_only": True,
        "actual_exchange_fills_verified": False,
        "account_funding_ledger_verified": False,
        "settlement_mark_price_verified": False,
        "untouched_holdout": False,
        "profitability_proven": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "interpretation": (
            "90-day equity windows are modelled capital-path returns, not independent "
            "out-of-sample trials. Closed-trade PnL by exit date is descriptive and "
            "cannot be added to window returns when open holdings exist. No venue fills, "
            "mark-price settlement, intraday liquidation or order-book impact verified."
        ),
    }
    result["journal_sha256"] = hashlib.sha256(canonical_json_bytes(result)).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise JournalError("refusing to overwrite existing journal")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    result = build_journal(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n",
                           encoding="utf-8")
    for item in result["comparisons"]:
        if item["period_id"] not in ("six_matched_window", "broad21_matched_window"):
            continue
        if (item["fee_bps_per_fill"], item["slippage_bps_per_side"]) not in COSTS:
            continue
        model = item["models"]["HOURLY_COLLISION_REFINED:UPPER_COST"]
        complete = [w for w in model["windows"] if w["complete_window"]]
        print("HISTORICAL_TRADE_TAPE", json.dumps({
            "period": item["period_id"], "fee": item["fee_bps_per_fill"],
            "slippage": item["slippage_bps_per_side"],
            "trades": model["closed_trades"],
            "total_return_pct": model["portfolio_return_pct"],
            "complete_90d_windows": len(complete),
            "positive_90d_windows": sum(number(w["equity_return_pct"]) > ZERO for w in complete),
            "worst_90d_return_pct": str(min(
                (number(w["equity_return_pct"]) for w in complete), default=ZERO
            )),
            "best_90d_return_pct": str(max(
                (number(w["equity_return_pct"]) for w in complete), default=ZERO
            )),
            "three_worst_trades": model["worst_closed_trades"][:3],
            "source_report_sha256": result["source_report_sha256"],
        }, default=str, sort_keys=True), flush=True)
    print("HISTORICAL_JOURNAL_DIGEST", result["journal_sha256"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
