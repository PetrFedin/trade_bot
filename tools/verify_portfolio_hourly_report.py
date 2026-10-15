"""Independently recompute material arithmetic in an emitted hourly replay report."""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes

D = Decimal
TOLERANCE = D("0.000001")


def near(a, b):
    if abs(D(a) - D(b)) > TOLERANCE:
        raise ValueError(f"independent monetary reconciliation failed: {a} != {b}")


def audit_report(report):
    unsigned = {k: v for k, v in report.items() if k != "evidence_sha256"}
    if hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest() != report["evidence_sha256"]:
        raise ValueError("report content digest mismatch")
    for field in ("strategy_promotion_allowed", "live_trading_allowed", "untouched_holdout",
                  "funding_cash_feedback_modelled", "provider_fills_verified"):
        if report[field] is not False:
            raise ValueError(f"unsupported authority/evidence claim: {field}")
    expected = {(u, f, s) for u in ("six", "broad21") for f, s in (("8", "5"), ("10", "10"))}
    observed = [(r["universe"], r["fee_bps_per_fill"], r["slippage_bps_per_side"])
                for r in report["comparisons"]]
    if len(observed) != 4 or set(observed) != expected:
        raise ValueError("missing, duplicate or unregistered comparison")
    for row in report["comparisons"]:
        for model, trades_field, equity_field in (
            ("control", "control_trades", "control_daily_equity"),
            ("hourly_refined", "refined_trades", "refined_daily_equity"),
        ):
            metrics = row[model]
            if metrics["closed_trades"] != len(row[trades_field]):
                raise ValueError("closed trade count mismatch")
            total = D("0")
            for trade in row[trades_field]:
                recomputed = (D(trade["gross_pnl_before_costs"]) - D(trade["slippage_cost"])
                              - D(trade["entry_fee"]) - D(trade["exit_fee"]))
                near(trade["net_pnl"], recomputed)
                total += recomputed
            near(metrics["closed_net_pnl_before_funding_usd"], total)
            near(metrics["ending_cash_before_funding_usd"], D("10000") + total
                 - D(metrics["final_open_cost_including_entry_fee_usd"]))
            near(metrics["ending_equity_before_funding_usd"],
                 D(metrics["ending_cash_before_funding_usd"])
                 + D(metrics["final_open_marked_value_usd"]))
            near(metrics["ending_equity_before_funding_usd"], row[equity_field][-1])
            near(metrics["fees_paid_usd"], metrics["all_fill_fees_independently_reconstructed_usd"])
            for return_key, funding_key in (
                ("return_after_legacy_funding_lower", "legacy_funding_cost_upper_usd"),
                ("return_after_legacy_funding_upper", "legacy_funding_cost_lower_usd"),
            ):
                near(metrics[return_key],
                     (D(metrics["ending_equity_before_funding_usd"])
                      - D(metrics[funding_key]) - D("10000")) / D("10000"))
            if (D(metrics["legacy_funding_cost_lower_usd"])
                    > D(metrics["legacy_funding_cost_upper_usd"])):
                raise ValueError("funding scenario order reversed")
        near(row["delta_return_after_legacy_funding_lower"],
             D(row["hourly_refined"]["return_after_legacy_funding_lower"])
             - D(row["control"]["return_after_legacy_funding_lower"]))
        if not row["all_reached_collisions_sourced"]:
            raise ValueError("comparison still contains unsourced reached collision")
        if sum(row["reached_collision_counts"].values()) != len(row["collision_audit"]):
            raise ValueError("collision audit population mismatch")
    return {"arithmetic_verification": "PASS", "comparisons": 4,
            "strategy_promotion_allowed": False, "evidence_sha256": report["evidence_sha256"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("report", type=Path)
    args = p.parse_args()
    print(json.dumps(audit_report(json.loads(args.report.read_text())), indent=2))


if __name__ == "__main__":
    main()
