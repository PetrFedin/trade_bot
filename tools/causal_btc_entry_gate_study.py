"""Fixed BTC regime entry-gate experiments on unchanged shared-cash history.

Research only. Gates block NEW entries based solely on BTC prior UTC closes,
never use current-day high/low, never force-close existing portfolio holdings,
and never select a winner or promote a live strategy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal as D
from pathlib import Path

from tools.independent_coin_strategy_lab import BROAD21, COSTS, SIX, canonical, read_candles
from tools.shared_capital_candidate_study import (
    CANDIDATES,
    RANKERS,
    replay_shared,
    study_shared,
)

BASELINE_SHA = "e2f8c37d5c6df948dccfbb054ad2941a443ba2b5b3015bdfadfee44ee82f9613"
GATES = ("BTC_SMA50_BLOCK", "BTC_VOLATILITY_100_BLOCK")


class GateStudyError(ValueError):
    """Fail-closed research data or causal entry-gate validation."""


def build_gate_report(archive: Path) -> dict:
    frozen = study_shared(archive)
    if frozen["report_sha256"] != BASELINE_SHA:
        raise GateStudyError("original ungated shared-cash model changed")
    first, last = frozen["first_utc"], frozen["last_utc"]
    raw = {sym: read_candles(archive / f"{sym}_D.csv", sym) for sym in BROAD21}
    universe = {
        sym: [c for c in bars if first <= c.day.isoformat() <= last]
        for sym, bars in raw.items()
    }
    clock = [x.day.isoformat() for x in universe["BTCUSDT"]]
    if len(clock) != 981 or any(
        [x.day.isoformat() for x in bars] != clock for bars in universe.values()
    ):
        raise GateStudyError("21-coin matched market clocks have changed")
    original = {
        (m["universe"], m["arm"], m["entry_ranker"],
         m["fee_bps_per_fill"], m["slippage_bps_per_fill"]): m
        for m in frozen["scenarios"]
    }
    if len(original) != 36:
        raise GateStudyError("baseline scenario identity is ambiguous")
    rows = []
    for label, symset in (("SIX", SIX), ("BROAD21", BROAD21)):
        data = {sym: universe[sym] for sym in symset}
        for gate in GATES:
            for arm in CANDIDATES:
                for ranker in RANKERS:
                    for fee, slip in COSTS:
                        result = replay_shared(
                            data, arm, ranker=ranker, fee_bps=fee,
                            slippage_bps=slip, entry_gate=gate,
                        )
                        if result.get("entry_gate_policy") != gate:
                            raise GateStudyError("variant gate missing in model")
                        key = (label, arm, ranker, str(fee), str(slip))
                        control = original[key]
                        if result["strategy_promotion_allowed"] is not False:
                            raise GateStudyError("gate acquired order permission")
                        if result["closed_trade_count"] != len(result["closed_trades"]):
                            raise GateStudyError("gate model trade count mismatch")
                        if abs(
                            D(result["ending_equity_usdt"]) - D("10000")
                            - D(result["closed_trade_net_usdt"])
                        ) > D("0.000001") * max(1, result["closed_trade_count"]):
                            raise GateStudyError("gated common cash no longer reconciles")
                        rows.append({
                            "universe": label,
                            "gate": gate,
                            "arm": arm,
                            "entry_ranker": ranker,
                            "fee_bps_per_fill": str(fee),
                            "slippage_bps_per_fill": str(slip),
                            "baseline_return_pct": control["return_pct"],
                            "gate_minus_original_percentage_points": str(
                                D(result["return_pct"]) - D(control["return_pct"])
                            ),
                            "baseline_drawdown_pct":
                                control["max_daily_close_drawdown_pct"],
                            "baseline_closed_trades":
                                control["closed_trade_count"],
                            "gate_blocked_signals":
                                result["entry_block_counts"].get(
                                    "BTC_PRIOR_CLOSE_ENTRY_GATE", 0
                                ),
                            "model": result,
                        })
    if len(rows) != 64:
        raise GateStudyError("two fixed BTC gates did not produce 64 cases")
    unique = {
        (r["universe"], r["gate"], r["arm"], r["entry_ranker"],
         r["fee_bps_per_fill"], r["slippage_bps_per_fill"]) for r in rows
    }
    if len(unique) != 64:
        raise GateStudyError("duplicate BTC entry-gate scenario")
    report = {
        "schema_version": "astra-previous-close-btc-entry-gate-study-v1",
        "evaluation_class": "INSPECTED_HISTORY_TWO_FIXED_RESEARCH_GATES",
        "baseline_shared_report_sha256": frozen["report_sha256"],
        "source_sha256": frozen["source_sha256"],
        "original_first_utc": first,
        "original_last_utc": last,
        "historical_days": 930,
        "scenario_count": len(rows),
        "capital_per_portfolio_usdt": "10000",
        "max_positions": 2,
        "opening_exposure_limit_fraction": "0.60",
        "BTC_gate_thresholds_frozen_before_results": {
            "BTC_SMA50_BLOCK":
                "Do not open new positions if last completed BTC close < prior SMA50",
            "BTC_VOLATILITY_100_BLOCK":
                "Do not open new positions if prior 20 BTC daily close return "
                "population standard deviation * sqrt(365) > 100% annualized",
        },
        "original_strategy_unchanged": True,
        "source_qualified_against_original_SHA": True,
        "hypothesis_not_optimized_to_return": True,
        "current_public_orderbook_used_as_historic": False,
        "account_fee_tier_verified": False,
        "genuine_historical_mark_price_verified": False,
        "funding_cash_settlement_verified": False,
        "broker_fills_verified": False,
        "untouched_future_holdout": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "profitability_proven": False,
        "scenarios": rows,
        "limitations": [
            "Filter operates only at entry and does not reduce existing exposure",
            "Both thresholds fixed before seeing gated trade outcomes",
            "21-coin archive and original Strategy A period already inspected",
            "No live venue depth, genuine execution fees, Mark Price or funding cash",
            "Daily OHLC stop-first does not prove intraday touch chronology",
            "A selected successful historical gate is NOT a forecast or approval",
        ],
    }
    report["report_sha256"] = hashlib.sha256(canonical(report)).hexdigest()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise GateStudyError("gated research report cannot replace an existing file")
    from tools.publish_research_receipt import publish_new_json_receipt

    report = build_gate_report(args.bars_dir)
    publish_new_json_receipt(args.output, report)
    for row in report["scenarios"]:
        x = row["model"]
        print("BTC_GATE_RESEARCH", json.dumps({
            "universe": row["universe"], "gate": row["gate"],
            "arm": row["arm"], "priority": row["entry_ranker"],
            "cost": [row["fee_bps_per_fill"], row["slippage_bps_per_fill"]],
            "return_pct": x["return_pct"],
            "drawdown_pct": x["max_daily_close_drawdown_pct"],
            "blocked": row["gate_blocked_signals"],
            "delta_vs_original_pp": row["gate_minus_original_percentage_points"],
        }, sort_keys=True))
    print("BTC_GATE_RESEARCH_SHA256", report["report_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
