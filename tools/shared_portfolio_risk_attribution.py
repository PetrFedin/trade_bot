"""Ex-post audit of already verified shared-cash trading research.

BTC regimes use only previous UTC closes. No model selection, exchange orders,
or predictive inference; all negative historical outcomes remain visible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tools.independent_coin_strategy_lab import BROAD21, canonical, read_candles

D = Decimal
ZERO = D("0")
ONE = D("1")
CASH = D("10000")
REGIMES = ("BTC_UPTREND", "BTC_DOWNTREND", "BTC_MIXED")
REASONS = {
    "STOP", "STOP_FIRST", "GAP_STOP", "TARGET", "GAP_TARGET",
    "TIME_EXIT", "ARCHIVE_END_CLOSE",
}


class RiskAuditError(ValueError):
    """Fail closed on unknown source, trade or cash ledger discrepancies."""


def num(value: object, label: str) -> D:
    if isinstance(value, bool):
        raise RiskAuditError(f"{label}: boolean is not numeric evidence")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RiskAuditError(f"{label}: invalid decimal") from exc
    if not result.is_finite():
        raise RiskAuditError(f"{label}: nonfinite value")
    return result


def check(actual: D, expected: D, label: str, tolerance: D = D("0.000001")):
    if abs(actual - expected) > tolerance:
        raise RiskAuditError(f"{label}: financial evidence mismatch")


def digest(data: dict) -> str:
    return hashlib.sha256(canonical(data)).hexdigest()


def verify_sources(original: dict, btc_path: Path):
    if not isinstance(original, dict):
        raise RiskAuditError("shared-cash source is not an object")
    given_sha = original.get("report_sha256")
    if not isinstance(given_sha, str) or given_sha != digest({
        k: v for k, v in original.items() if k != "report_sha256"
    }):
        raise RiskAuditError("source report hash differs")
    if (
        original.get("schema_version") != "astra-shared-capital-candidate-study-v1"
        or original.get("scenario_count") != 36
        or original.get("shared_days") != 981
        or tuple(original.get("symbols", [])) != BROAD21
    ):
        raise RiskAuditError("unsupported or incomplete research population")
    for flag in (
        "actual_fee_tier_verified", "funding_settlement_verified",
        "true_market_fills_verified", "unseen_holdout",
        "strategy_promotion_allowed", "live_trading_allowed",
        "profitability_proven", "current_orderbook_mixed_into_history",
    ):
        if original.get(flag) is not False:
            raise RiskAuditError("research authorization boundary differs")
    if not btc_path.is_file() or btc_path.is_symlink():
        raise RiskAuditError("missing or symlinked raw BTC archive")
    expected = original["source_sha256"]["BTCUSDT_D.csv"]
    if hashlib.sha256(btc_path.read_bytes()).hexdigest() != expected:
        raise RiskAuditError("original BTC archive differs from source SHA")
    bars = read_candles(btc_path, "BTCUSDT")
    first, last = original["first_utc"], original["last_utc"]
    matched = [bar for bar in bars if first <= bar.day.isoformat() <= last]
    days = [bar.day.isoformat() for bar in matched]
    if (
        len(days) != 981
        or days != sorted(set(days))
        or days[0] != first
        or days[-1] != last
    ):
        raise RiskAuditError("historical BTC regime input is misaligned")
    return matched


def prior_close_regime(bars, index: int) -> str:
    """No entry-day close/high/low access; immutable ex-ante classification."""
    if index < 51 or index > len(bars):
        raise RiskAuditError("prior-close regime warmup is insufficient")
    past = bars[:index]
    close = past[-1].close
    sma50 = sum((b.close for b in past[-50:]), ZERO) / 50
    return20 = close / past[-21].close - ONE
    if close > sma50 and return20 >= D("0.05"):
        return "BTC_UPTREND"
    if close < sma50 and return20 <= D("-0.05"):
        return "BTC_DOWNTREND"
    return "BTC_MIXED"


def _trade_breakdown(model: dict) -> dict:
    coin = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": ZERO})
    exits = Counter()
    gross_net = fees = slippage = positive = negative = ZERO
    individual = []
    for trade in model["closed_trades"]:
        if trade["symbol"] not in model["symbols"]:
            raise RiskAuditError("unregistered historical trade instrument")
        reason = trade["exit_reason"]
        if reason not in REASONS or trade["entry_utc"] > trade["exit_utc"]:
            raise RiskAuditError("bad trade chronology or exit reason")
        gross = num(trade["gross_usdt"], "gross trade")
        fee = num(trade["fees_usdt"], "commission")
        slip = num(trade["slippage_usdt"], "slippage")
        net = num(trade["net_usdt"], "net trade")
        if fee < ZERO or slip < ZERO:
            raise RiskAuditError("negative execution costs are unqualified")
        check(gross - fee - slip, net, "gross minus actual modeled costs")
        gross_net += net
        fees += fee
        slippage += slip
        positive += max(ZERO, net)
        negative += min(ZERO, net)
        item = coin[trade["symbol"]]
        item["trades"] += 1
        item["wins"] += int(net > ZERO)
        item["pnl"] += net
        individual.append(net)
        exits[reason] += 1
    if len(individual) != model["closed_trade_count"]:
        raise RiskAuditError("closed trade population differs")
    check(gross_net, num(model["closed_trade_net_usdt"], "realized"), "trade sum")
    check(fees, num(model["gross_fee_paid_usdt"], "fees"), "fee sum")
    check(slippage, num(model["gross_slippage_paid_usdt"], "slips"), "slip sum")
    check(gross_net, num(model["ending_equity_usdt"], "terminal") - CASH,
          "cash and realized pnl")
    stops = sum(exits[k] for k in ("STOP", "STOP_FIRST", "GAP_STOP"))
    bottom = sorted(individual)[:max(1, math.ceil(len(individual) * .1))]
    return {
        "closed_trade_count": len(individual),
        "stop_exit_pct": str(D(100 * stops) / len(individual))
        if individual else None,
        "profit_factor_ex_funding": str(positive / abs(negative))
        if negative < ZERO else None,
        "worst_10pct_closed_trades_total_usdt": str(sum(bottom, ZERO))
        if individual else None,
        "fee_usdt": str(fees),
        "slippage_usdt": str(slippage),
        "per_coin": {
            sym: {"trades": row["trades"], "wins": row["wins"],
                  "net_usdt": str(row["pnl"])}
            for sym, row in sorted(coin.items())
        },
        "exit_reasons": dict(sorted(exits.items())),
    }


def _model_risk(model: dict, clock: dict[str, str]) -> dict:
    marks, traces = model["daily_equity"], model["decision_trace"]
    if len(marks) != 930 or len(traces) != 930:
        raise RiskAuditError("missing or stale historical equity evidence")
    prev = peak = CASH
    underwater = longest = 0
    biggest_dd = ZERO
    daily_returns = []
    daily_pnl = ZERO
    rejects = Counter()
    regimes = {label: {"days": 0, "pnl": ZERO, "returns": ZERO}
               for label in REGIMES}
    for mark, trace in zip(marks, traces, strict=True):
        day = mark["day"]
        if trace["day"] != day or day not in clock:
            raise RiskAuditError("decision, book and BTC regime day differ")
        equity = num(mark["equity"], "marked equity")
        free = num(mark["free_cash"], "free cash")
        gross = num(mark["gross_close_value"], "marked position")
        check(free + gross, equity, "cash and asset bridge")
        if equity <= ZERO or free < ZERO or len(mark["open_symbols"]) > 2:
            raise RiskAuditError("nonpositive capital or invalid open slots")
        delta = equity - prev
        pct = D(100) * (equity / prev - ONE)
        label = clock[day]
        item = regimes[label]
        item["days"] += 1
        item["pnl"] += delta
        item["returns"] += pct
        daily_pnl += delta
        daily_returns.append(pct)
        peak = max(peak, equity)
        underwater = underwater + 1 if equity < peak else 0
        longest = max(longest, underwater)
        biggest_dd = max(biggest_dd, (peak - equity) / peak)
        rejects.update(trace["entry_rejections"].values())
        prev = equity
    check(daily_pnl, prev - CASH, "daily equity attribution")
    check(prev, num(model["ending_equity_usdt"], "last equity"),
          "source model final equity")
    check(D(100) * biggest_dd, num(model["max_daily_close_drawdown_pct"], "drawdown"),
          "recomputed drawdown", D("0.0000001"))
    if dict(sorted(rejects.items())) != model["entry_block_counts"]:
        raise RiskAuditError("blocked signal decision trace differs")
    check(sum((v["pnl"] for v in regimes.values()), ZERO), prev - CASH,
          "BTC regime profit decomposition")
    population = math.ceil(len(daily_returns) * .05)
    tail = sorted(daily_returns)[:population]
    return {
        "universe": model["universe"],
        "arm": model["arm"],
        "ranker": model["entry_ranker"],
        "fee_bps": model["fee_bps_per_fill"],
        "slippage_bps": model["slippage_bps_per_fill"],
        "return_pct": model["return_pct"],
        "max_drawdown_pct": model["max_daily_close_drawdown_pct"],
        "worst_day_pct": str(min(daily_returns)),
        "mean_worst_5pct_days_pct": str(sum(tail, ZERO) / len(tail)),
        "worst_5pct_day_count": len(tail),
        "longest_daily_close_underwater_days": longest,
        "BTC_previous_close_regimes": {
            name: {
                "days": part["days"], "marked_equity_change_usdt": str(part["pnl"]),
                "average_daily_return_pct": str(part["returns"] / part["days"])
                if part["days"] else None,
            } for name, part in regimes.items()
        },
        "rejected_entries": dict(sorted(rejects.items())),
        "trades": _trade_breakdown(model),
        "daily_cash_audited": True,
        "regime_cash_audited": True,
    }


def build_report(shared: dict, btc_bars) -> dict:
    if shared.get("scenario_count") != 36:
        raise RiskAuditError("incomplete shared capital cohort")
    history = [bar.day.isoformat() for bar in btc_bars]
    if len(history) != 981:
        raise RiskAuditError("BTC matched time spine differs")
    clock = {history[i]: prior_close_regime(btc_bars, i)
             for i in range(51, len(history))}
    if [x["day"] for x in shared["scenarios"][0]["daily_equity"]] != history[51:]:
        raise RiskAuditError("BTC previous-close clock not synchronized")
    models = [_model_risk(m, clock) for m in shared["scenarios"]]
    combos = {(m["universe"], m["arm"], m["ranker"],
               m["fee_bps"], m["slippage_bps"]) for m in models}
    if len(combos) != 36:
        raise RiskAuditError("duplicate or missing research model")
    pairs = []
    for low in models:
        if (low["fee_bps"], low["slippage_bps"]) != ("8", "5"):
            continue
        match = [high for high in models if high["universe"] == low["universe"]
                 and high["arm"] == low["arm"] and high["ranker"] == low["ranker"]
                 and (high["fee_bps"], high["slippage_bps"]) == ("10", "10")]
        if len(match) != 1:
            raise RiskAuditError("missing independently fixed cost comparison")
        high = match[0]
        pairs.append({
            "universe": low["universe"], "arm": low["arm"],
            "ranker": low["ranker"],
            "low_cost_return_pct": low["return_pct"],
            "high_cost_return_pct": high["return_pct"],
            "high_minus_low_percentage_points": str(
                num(high["return_pct"], "high") - num(low["return_pct"], "low")
            ),
        })
    if len(pairs) != 18:
        raise RiskAuditError("missing fee/slippage stress pair")
    report = {
        "schema_version": "astra-shared-portfolio-risk-attribution-v1",
        "evidence_class": "REUSED_HISTORY_EX_POST_DESCRIPTIVE_ONLY",
        "source_shared_sha256": shared["report_sha256"],
        "source_btc_sha256": shared["source_sha256"]["BTCUSDT_D.csv"],
        "scenario_count": len(models),
        "cost_comparison_count": len(pairs),
        "historical_days": 930,
        "BTC_regime_definition": (
            "At day T open classify only previous BTC UTC close vs "
            "previous SMA50 and previous 20-day return +/-5%. "
            "Retrospective attribution, never a trading signal."
        ),
        "BTC_regime_days": dict(sorted(Counter(clock.values()).items())),
        "models": models,
        "cost_pairs": pairs,
        "source_sha_reconciled": True,
        "historical_mark_price_verified": False,
        "real_funding_settlement_verified": False,
        "account_fee_tier_verified": False,
        "historical_order_book_verified": False,
        "unseen_holdout": False,
        "profitability_proven": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "limitations": [
            "All historic returns inspected before analysis, no untouched OOS",
            "BTC classifications based only on previous completed day",
            "Regime PnL attribution is descriptive, not causal evidence",
            "Worst 5pct days is empirical mean, not predictive risk limit",
            "No account funding, Mark Price settlements or true venue fills",
            "Daily closing equity hides intraday drawdown and liquidation",
        ],
    }
    report["report_sha256"] = digest(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared", type=Path, required=True)
    parser.add_argument("--btc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.shared.is_file() or args.shared.is_symlink():
        raise RiskAuditError("missing or linked shared research source")
    if args.output.exists() or args.output.is_symlink():
        raise RiskAuditError("research receipts are immutable")
    original = json.loads(args.shared.read_text(encoding="utf-8"))
    btc = verify_sources(original, args.btc)
    report = build_report(original, btc)
    from tools.publish_research_receipt import publish_new_json_receipt

    publish_new_json_receipt(args.output, report)
    print("ASTRA_RISK_ATTRIBUTION", json.dumps({
        "models": report["scenario_count"],
        "cost_comparisons": report["cost_comparison_count"],
        "regimes": report["BTC_regime_days"],
        "sha256": report["report_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
