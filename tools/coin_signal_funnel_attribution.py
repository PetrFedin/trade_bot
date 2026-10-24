"""Source-pinned per-coin signal-to-trade funnel for ASTRA research.

Audits 36 unchanged shared-cash portfolios and 64 BTC entry-gate variants.
Rejected signals have unknown counterfactual PnL: never value as missed profit.
Read-only, historical ex-post research. No order or strategy promotion API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal as D
from pathlib import Path

from tools.causal_btc_entry_gate_study import BASELINE_SHA, GATES
from tools.independent_coin_strategy_lab import BROAD21, COSTS, SIX, canonical
from tools.shared_capital_candidate_study import CANDIDATES, RANKERS
from tools.shared_portfolio_risk_attribution import (
    prior_close_regime,
    verify_sources,
)

GATE_SHA = "04ae594ea9d67cdb85363910761de86f91fc9b0b779904c0914d372298b5b75c"
RISK_SHA = "f29cd1a860d7df82c6e72ffa895d4e5daafb7be40ece08bd9171674024761087"
ZERO = D("0")
CASH = D("10000")
TOL = D("0.000001")
REJECT_REASONS = (
    "MAX_TWO_POSITIONS", "ENTRY_EXPOSURE_CAP",
    "MIN_ALLOCATION_NOT_REACHED", "BTC_PRIOR_CLOSE_ENTRY_GATE",
)
REGIMES = ("BTC_UPTREND", "BTC_DOWNTREND", "BTC_MIXED")


class FunnelAuditError(ValueError):
    """Reject inconsistent source identity, chronology or financial claims."""


def dec(x: object) -> D:
    if isinstance(x, bool):
        raise FunnelAuditError("invalid boolean for financial numeric value")
    try:
        result = D(str(x))
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise FunnelAuditError("invalid financial numeric value") from exc
    if not result.is_finite():
        raise FunnelAuditError("nonfinite financial evidence")
    return result


def near(x: D, y: D, what: str, tol: D = TOL) -> None:
    if abs(x - y) > tol:
        raise FunnelAuditError(f"{what}: original cash evidence differs")


def sha(doc: dict) -> str:
    return hashlib.sha256(canonical(doc)).hexdigest()


def verify(doc: dict, expected: str, tag: str) -> None:
    if not isinstance(doc, dict):
        raise FunnelAuditError(f"{tag} must be original dictionary")
    embedded = doc.get("report_sha256")
    if (
        embedded != expected
        or sha({k: v for k, v in doc.items() if k != "report_sha256"}) != expected
    ):
        raise FunnelAuditError(f"{tag} pinned financial report SHA mismatch")


def _fresh_row() -> dict:
    return {
        "signals": 0, "entered": 0, "rejections": Counter(),
        "trades": 0, "wins": 0, "gross": ZERO,
        "fees": ZERO, "slippage": ZERO, "net": ZERO,
        "exits": Counter(), "entry_regimes": {
            name: {"trades": 0, "net": ZERO} for name in REGIMES
        },
    }


def analyze_model(
    model: dict, *, label: str, gate: str,
    clock: dict[str, str], original_days: tuple[str, ...],
) -> dict:
    if label not in ("SIX", "BROAD21"):
        raise FunnelAuditError("unregistered portfolio research universe")
    expected_symbols = SIX if label == "SIX" else BROAD21
    # Original 36 wrapper includes symbols; the 64-gate child model does not.
    # Gate universe comes from an immutable, SHA-pinned enclosing scenario.
    symbols = tuple(model.get("symbols", expected_symbols))
    if symbols != expected_symbols:
        raise FunnelAuditError("unknown or reordered research coin universe")
    traces = model["decision_trace"]
    marks = model["daily_equity"]
    if len(traces) != len(marks) or len(traces) != len(original_days):
        raise FunnelAuditError("historical decision/equity timeline missing")
    if model.get("live_trading_allowed") is not False:
        raise FunnelAuditError("live trade authorization leaked into source")
    if model.get("strategy_promotion_allowed") is not False:
        raise FunnelAuditError("research strategy promotion leaked into source")
    if model.get("historical_orderbook_included") is not False:
        raise FunnelAuditError("false historical orderbook execution attribution")
    if model.get("historical_funding_included") is not False:
        raise FunnelAuditError("false funding execution attribution")
    if gate == "NONE" and model.get("entry_gate_policy") is not None:
        raise FunnelAuditError("original model unexpectedly gated")
    if gate != "NONE" and model.get("entry_gate_policy") != gate:
        raise FunnelAuditError("historical experimental gate identity differs")
    rows = {sym: _fresh_row() for sym in symbols}
    traced_entries = Counter()
    traced_exits = Counter()
    all_blocks = Counter()
    previous_active: set[str] = set()
    total_signals = selected_total = 0
    for index, (decision, mark) in enumerate(zip(traces, marks, strict=True)):
        day = original_days[index]
        if (
            decision["day"] != day or mark["day"] != day
            or day not in clock
        ):
            raise FunnelAuditError("chronology or previous-close regime mismatch")
        ranked = decision["ranked_prior_day_signals"]
        accepted = decision["entered"]
        exited = decision["exited"]
        rejected = decision["entry_rejections"]
        if (
            not isinstance(ranked, list) or not isinstance(accepted, list)
            or not isinstance(exited, list) or not isinstance(rejected, dict)
            or len(ranked) != len(set(ranked))
            or len(accepted) != len(set(accepted))
            or len(exited) != len(set(exited))
        ):
            raise FunnelAuditError("duplicated or corrupted daily coin signal")
        ranked_set = set(ranked)
        entered_set = set(accepted)
        rejected_set = set(rejected)
        if (
            not ranked_set.issubset(rows)
            or entered_set & rejected_set
            or ranked_set != entered_set | rejected_set
            or ranked_set & previous_active
            or any(reason not in REJECT_REASONS for reason in rejected.values())
        ):
            raise FunnelAuditError("signal cannot be traced to entry or rejection")
        if gate == "NONE" and "BTC_PRIOR_CLOSE_ENTRY_GATE" in rejected.values():
            raise FunnelAuditError("baseline contains experimental blocked signal")
        if not set(exited).issubset(previous_active | entered_set):
            raise FunnelAuditError("exited position without previous live holding")
        current = (previous_active | entered_set) - set(exited)
        if (
            len(current) > 2 or current != set(mark["open_symbols"])
            or len(mark["open_symbols"]) != len(current)
        ):
            raise FunnelAuditError("position state disagrees with daily cash book")
        previous_active = current
        cash = dec(mark["free_cash"])
        gross = dec(mark["gross_close_value"])
        equity = dec(mark["equity"])
        if cash < ZERO or gross < ZERO or equity <= ZERO:
            raise FunnelAuditError("negative cash or invalid long-only portfolio")
        near(cash + gross, equity, "end-of-day cash + position value")
        total_signals += len(ranked)
        selected_total += len(accepted)
        for sym in ranked:
            rows[sym]["signals"] += 1
        for sym in accepted:
            rows[sym]["entered"] += 1
            traced_entries[sym, day] += 1
        for sym in exited:
            traced_exits[sym, day] += 1
        for sym, reason in rejected.items():
            rows[sym]["rejections"][reason] += 1
            all_blocks[reason] += 1
    if previous_active or model["terminal_open_positions"]:
        raise FunnelAuditError("research endpoint is not flat")
    if dict(sorted(all_blocks.items())) != model["entry_block_counts"]:
        raise FunnelAuditError("rejection reasons differ from verified day trace")
    if len(model["closed_trades"]) != model["closed_trade_count"]:
        raise FunnelAuditError("closed trade count differs from original model")
    seen_entries = Counter()
    seen_exits = Counter()
    gross_total = fee_total = slip_total = net_total = ZERO
    for trade in model["closed_trades"]:
        sym = trade["symbol"]
        entry, exit_day = trade["entry_utc"], trade["exit_utc"]
        if (
            sym not in rows or entry not in clock or exit_day not in clock
            or entry > exit_day
        ):
            raise FunnelAuditError("closed trade has unknown coin or timestamp")
        seen_entries[sym, entry] += 1
        seen_exits[sym, exit_day] += 1
        gross = dec(trade["gross_usdt"])
        fee = dec(trade["fees_usdt"])
        slip = dec(trade["slippage_usdt"])
        net = dec(trade["net_usdt"])
        if fee < ZERO or slip < ZERO:
            raise FunnelAuditError("negative execution expenses")
        near(gross - fee - slip, net, "closed trade net PnL")
        row = rows[sym]
        row["trades"] += 1
        row["wins"] += int(net > ZERO)
        row["gross"] += gross
        row["fees"] += fee
        row["slippage"] += slip
        row["net"] += net
        row["exits"][trade["exit_reason"]] += 1
        cohort = row["entry_regimes"][clock[entry]]
        cohort["trades"] += 1
        cohort["net"] += net
        gross_total += gross
        fee_total += fee
        slip_total += slip
        net_total += net
    if seen_entries != traced_entries or seen_exits != traced_exits:
        raise FunnelAuditError("entry/exit daily events do not match real model tape")
    near(net_total, dec(model["closed_trade_net_usdt"]), "all closed trades")
    near(net_total + CASH, dec(model["ending_equity_usdt"]), "ending cash")
    near(fee_total, dec(model["gross_fee_paid_usdt"]), "total commissions")
    near(slip_total, dec(model["gross_slippage_paid_usdt"]), "slippage")
    if total_signals != selected_total + sum(all_blocks.values()):
        raise FunnelAuditError("signal funnel does not reconcile")
    out = []
    for sym, row in rows.items():
        total_rejected = sum(row["rejections"].values())
        if row["signals"] != row["entered"] + total_rejected:
            raise FunnelAuditError("per-coin opportunity count differs")
        if row["trades"] != row["entered"]:
            raise FunnelAuditError("unreconciled per-coin entries and trade tape")
        near(row["gross"] - row["fees"] - row["slippage"], row["net"],
             "instrument net result")
        near(sum((co["net"] for co in row["entry_regimes"].values()), ZERO),
             row["net"], "entry regime and per-coin trade net")
        out.append({
            "symbol": sym,
            "eligible_signal_days": row["signals"],
            "accepted_entries": row["entered"],
            "rejected_signal_days": total_rejected,
            "accepted_signal_fraction": str(
                D(row["entered"]) / row["signals"]
            ) if row["signals"] else None,
            "rejection_reasons": dict(sorted(row["rejections"].items())),
            "closed_trades": row["trades"],
            "winning_trades": row["wins"],
            "win_rate_fraction": str(D(row["wins"]) / row["trades"])
            if row["trades"] else None,
            "gross_usdt": str(row["gross"]),
            "fees_usdt": str(row["fees"]),
            "slippage_usdt": str(row["slippage"]),
            "net_usdt": str(row["net"]),
            "average_net_per_closed_trade_usdt": str(
                row["net"] / row["trades"]
            ) if row["trades"] else None,
            "exits": dict(sorted(row["exits"].items())),
            "entry_regimes": {
                state: {
                    "closed_trades_entered_in_regime": d["trades"],
                    "eventual_net_trade_pnl_usdt": str(d["net"]),
                } for state, d in row["entry_regimes"].items()
            },
            "rejected_signal_counterfactual_pnl": None,
        })
    return {
        "universe": label,
        "arm": model["arm"],
        "entry_ranker": model["entry_ranker"],
        "gate": gate,
        "fee_bps_per_side": model["fee_bps_per_fill"],
        "slippage_bps_per_side": model["slippage_bps_per_fill"],
        "original_model_return_pct": model["return_pct"],
        "original_model_drawdown_pct": model["max_daily_close_drawdown_pct"],
        "eligible_signals": total_signals,
        "accepted_signals": selected_total,
        "rejected_signals": sum(all_blocks.values()),
        "rejection_reasons": dict(sorted(all_blocks.items())),
        "closed_trades": model["closed_trade_count"],
        "realized_gross_usdt": str(gross_total),
        "realized_fee_usdt": str(fee_total),
        "realized_slippage_usdt": str(slip_total),
        "realized_net_usdt": str(net_total),
        "per_coin": out,
        "rejected_signal_counterfactual_pnl": None,
    }


def build_report(original: dict, gates: dict, risk: dict, btc_path: Path) -> dict:
    verify(original, BASELINE_SHA, "original shared capital")
    verify(gates, GATE_SHA, "frozen 64-gate variants")
    verify(risk, RISK_SHA, "BTC risk attribution")
    if (
        original.get("scenario_count") != 36 or gates.get("scenario_count") != 64
        or risk.get("scenario_count") != 36
        or original.get("shared_days") != 981 or risk.get("historical_days") != 930
        or gates.get("historical_days") != 930
        or gates.get("baseline_shared_report_sha256") != BASELINE_SHA
        or risk.get("source_shared_sha256") != BASELINE_SHA
        or gates.get("source_sha256") != original.get("source_sha256")
        or risk.get("source_btc_sha256")
        != original["source_sha256"]["BTCUSDT_D.csv"]
    ):
        raise FunnelAuditError("mismatched source or cash study scope")
    for doc, names in (
        (original, ("live_trading_allowed", "profitability_proven",
                    "strategy_promotion_allowed", "unseen_holdout")),
        (gates, ("live_trading_allowed", "profitability_proven",
                 "strategy_promotion_allowed", "untouched_future_holdout")),
        (risk, ("live_trading_allowed", "profitability_proven",
                "strategy_promotion_allowed", "unseen_holdout")),
    ):
        if any(doc.get(name) is not False for name in names):
            raise FunnelAuditError("research-only authority mismatch")
    btc = verify_sources(original, btc_path)
    days = tuple(bar.day.isoformat() for bar in btc)[51:]
    if len(days) != 930:
        raise FunnelAuditError("missing BTC market regime timeline")
    clock = {
        btc[i].day.isoformat(): prior_close_regime(btc, i)
        for i in range(51, len(btc))
    }
    original_keys = {}
    rows = []
    for model in original["scenarios"]:
        cohort = analyze_model(
            model, label=model["universe"], gate="NONE",
            clock=clock, original_days=days,
        )
        key = (
            cohort["universe"], cohort["arm"], cohort["entry_ranker"],
            cohort["fee_bps_per_side"], cohort["slippage_bps_per_side"],
        )
        if key in original_keys:
            raise FunnelAuditError("duplicate historical baseline")
        original_keys[key] = cohort
        rows.append(cohort)
    if len(original_keys) != 36:
        raise FunnelAuditError("original baseline model missing")
    variant_keys = set()
    for study in gates["scenarios"]:
        rule = study["gate"]
        key = (
            study["universe"], study["arm"], study["entry_ranker"],
            study["fee_bps_per_fill"], study["slippage_bps_per_fill"],
        )
        if key not in original_keys or rule not in GATES:
            raise FunnelAuditError("experimental model has no admitted baseline")
        identity = (key, rule)
        if identity in variant_keys:
            raise FunnelAuditError("duplicate experimental BTC gate")
        variant_keys.add(identity)
        cohort = analyze_model(
            study["model"], label=study["universe"], gate=rule,
            clock=clock, original_days=days,
        )
        ref = original_keys[key]
        if dec(study["baseline_return_pct"]) != dec(ref["original_model_return_pct"]):
            raise FunnelAuditError("wrong experimental baseline return")
        near(
            dec(study["gate_minus_original_percentage_points"]),
            dec(cohort["original_model_return_pct"])
            - dec(ref["original_model_return_pct"]),
            "reported experimental return delta", D("0.000000001"),
        )
        if (
            study["gate_blocked_signals"]
            != cohort["rejection_reasons"].get("BTC_PRIOR_CLOSE_ENTRY_GATE", 0)
        ):
            raise FunnelAuditError("missing counter of gated market opportunities")
        rows.append(cohort)
    if len(rows) != 100 or len(variant_keys) != 64:
        raise FunnelAuditError("incomplete historical candidate portfolio matrix")
    bykey = {
        (r["universe"], r["arm"], r["fee_bps_per_side"],
         r["slippage_bps_per_side"], r["gate"], r["entry_ranker"]): r
        for r in rows
    }
    if len(bykey) != 100:
        raise FunnelAuditError("duplicate portfolio opportunity record")
    ranker_pairs = []
    for universe, symbols in (("SIX", SIX), ("BROAD21", BROAD21)):
        for arm in CANDIDATES:
            for fee, slip in COSTS:
                for gate in ("NONE", *GATES):
                    key = (universe, arm, str(fee), str(slip), gate)
                    left = bykey[(*key, RANKERS[0])]
                    right = bykey[(*key, RANKERS[1])]
                    ranker_pairs.append({
                        "universe": universe, "symbols": len(symbols),
                        "arm": arm, "gate": gate,
                        "fee_bps": str(fee), "slip_bps": str(slip),
                        "alphabetic_return_pct": left["original_model_return_pct"],
                        "prior_20d_return_rank_return_pct":
                            right["original_model_return_pct"],
                        "prior_20d_minus_alphabetic_percentage_points": str(
                            dec(right["original_model_return_pct"])
                            - dec(left["original_model_return_pct"])
                        ),
                        "alphabetic_accepted": left["accepted_signals"],
                        "prior_20d_accepted": right["accepted_signals"],
                        "important": (
                            "Eligibility changes when earlier holdings differ; "
                            "candidate lists are not matched counterfactual fills."
                        ),
                    })
    if len(ranker_pairs) != 48:
        raise FunnelAuditError("missing exact ranker comparison")
    report = {
        "schema_version": "astra-coin-selection-opportunity-funnel-v1",
        "source_class": "REUSED_HISTORICAL_CASH_LEDGER_NO_COUNTERFACTUAL_PRICES",
        "original_shared_sha256": original["report_sha256"],
        "experimental_gate_sha256": gates["report_sha256"],
        "risk_regime_sha256": risk["report_sha256"],
        "raw_btc_sha256": original["source_sha256"]["BTCUSDT_D.csv"],
        "matched_daily_marks": len(days),
        "original_portfolios": 36,
        "gated_portfolios": 64,
        "total_model_scenarios": len(rows),
        "ranker_pair_count": len(ranker_pairs),
        "universe_original_sources": original["source_sha256"],
        "no_historical_rejected_signal_pnl_assumed": True,
        "scenario_records": rows,
        "ranker_pairs": ranker_pairs,
        "funding_settlements_verified": False,
        "historical_orderbook_verified": False,
        "account_fee_tier_verified": False,
        "real_exchange_fills_verified": False,
        "untouched_future_holdout": False,
        "live_trading_allowed": False,
        "strategy_promotion_allowed": False,
        "profitability_proven": False,
        "limitations": [
            "Skipped signals have NO measured counterfactual profit or loss",
            "Signal eligibility depends on prior holdings: rankers do not share "
            "an identical opportunity sample",
            "Eventual trade PnL grouped by entry-time BTC state, not exit state",
            "Regime classification uses previous completed BTC UTC candles",
            "All prices were previously inspected; no future OOS qualified",
            "No real exchange settlement, funding, book queue, or fills",
        ],
    }
    report["report_sha256"] = sha(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared", required=True, type=Path)
    parser.add_argument("--gates", required=True, type=Path)
    parser.add_argument("--risk", required=True, type=Path)
    parser.add_argument("--btc", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise FunnelAuditError("will not overwrite historical evidence receipt")
    for path in (args.shared, args.gates, args.risk, args.btc):
        if not path.is_file() or path.is_symlink():
            raise FunnelAuditError("missing or symlinked historical research source")
    shared = json.loads(args.shared.read_text(encoding="utf-8"))
    gated = json.loads(args.gates.read_text(encoding="utf-8"))
    risk = json.loads(args.risk.read_text(encoding="utf-8"))
    report = build_report(shared, gated, risk, args.btc)
    from tools.publish_research_receipt import publish_new_json_receipt

    publish_new_json_receipt(args.output, report)
    print("COIN_OPPORTUNITY_FUNNEL", json.dumps({
        "model_scenarios": report["total_model_scenarios"],
        "ranker_pairs": report["ranker_pair_count"],
        "raw_btc_sha256": report["raw_btc_sha256"],
        "receipt_sha256": report["report_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
