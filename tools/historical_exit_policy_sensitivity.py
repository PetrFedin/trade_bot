"""Compare predeclared exits on inspected history without parameter optimization.

The shipped portfolio, entry/selection rules, risk exposure and funding engine are
unchanged. No favorable arm can grant paper/live trading or an OOS claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.position_management import PositionManagementPolicy
from tools.historical_portfolio_evidence import build_evidence


def _arms() -> tuple[tuple[str, PositionManagementPolicy], ...]:
    shipped = PositionManagementPolicy()
    return (
        ("SHIPPED_STOP_2P", shipped),
        ("WIDER_STOP_3P", replace(shipped, stop_loss_fraction=Decimal("0.03"))),
        ("WIDER_STOP_4P", replace(shipped, stop_loss_fraction=Decimal("0.04"))),
    )


def _benchmark(evidence: dict, name: str) -> Decimal:
    matches = [Decimal(item["total_return"]) for item in evidence["benchmarks"]
               if item["benchmark_id"] == name]
    if len(matches) != 1:
        raise ValueError("required benchmark missing or ambiguous: " + name)
    return matches[0]


def run_fixed_exit_study(
    *,
    bars_dir: Path,
    funding_dir: Path,
    symbols: tuple[str, ...],
    fee_bps_per_fill: Decimal = Decimal("8"),
    slippage_bps: Decimal = Decimal("5"),
) -> dict:
    if not symbols or symbols != tuple(sorted(set(symbols))):
        raise ValueError("symbols must be nonempty, unique, and sorted")
    if not all(value.is_finite() and value >= 0
               for value in (fee_bps_per_fill, slippage_bps)):
        raise ValueError("cost assumptions must be finite and non-negative")

    arms: dict[str, dict[str, object]] = {}
    baseline: dict[str, object] | None = None
    shared_dataset: str | None = None
    for name, policy in _arms():
        e = build_evidence(
            bars_dir=bars_dir, funding_dir=funding_dir, symbols=symbols,
            opening_cash=Decimal("10000"), fee_per_fill=Decimal("0"),
            fee_bps_per_fill=fee_bps_per_fill, slippage_bps=slippage_bps,
            position_policy=policy,
        )
        if shared_dataset is None:
            shared_dataset = str(e["dataset_sha256"])
        if e["dataset_sha256"] != shared_dataset:
            raise ValueError("arm dataset identity diverged")
        if e["verdict"] != "PROFITABILITY_NOT_PROVEN":
            raise ValueError("research-only study cannot produce promotion evidence")

        net = Decimal(e["total_return"])
        lower = Decimal(e["cost_adjusted_return_lower_bound"])
        upper = Decimal(e["cost_adjusted_return_upper_bound"])
        matched = _benchmark(e, "equal_weight_capital_matched")
        row: dict[str, object] = {
            "stop_loss_fraction": str(policy.stop_loss_fraction),
            "take_profit_fraction": str(policy.take_profit_fraction),
            "strategy_config_sha256": e["strategy_config_sha256"],
            "evidence_sha256": e["evidence_sha256"],
            "closed_trades": e["closed_trade_count"],
            "total_return_before_funding": str(net),
            "funding_adjusted_lower": str(lower),
            "funding_adjusted_upper": str(upper),
            "capital_matched_benchmark": str(matched),
            "max_drawdown_fraction": e["max_drawdown_fraction"],
            "turnover_fraction": e["turnover_fraction"],
            "fees_paid": e["fees_paid"],
            "verdict": e["verdict"],
            "research_only": True,
        }
        if baseline is None:
            baseline = row
        row["delta_vs_shipped_pre_funding"] = str(
            net - Decimal(baseline["total_return_before_funding"])
        )
        row["delta_vs_shipped_funding_lower"] = str(
            lower - Decimal(baseline["funding_adjusted_lower"])
        )
        row["delta_vs_shipped_funding_upper"] = str(
            upper - Decimal(baseline["funding_adjusted_upper"])
        )
        arms[name] = row

    if len({str(item["strategy_config_sha256"]) for item in arms.values()}) != len(arms):
        raise ValueError("distinct exit policies must have distinct configuration hashes")
    payload: dict[str, object] = {
        "schema_version": "astra-fixed-exit-policy-sensitivity-research-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_EXPLORATORY",
        "research_only": True,
        "parameter_optimization_performed": False,
        "no_winner_selected": True,
        "strategy_promotion_allowed": False,
        "live_execution_allowed": False,
        "market_data_source": "COMMITTED_BYBIT_DAILY_SNAPSHOTS",
        "dataset_sha256": shared_dataset,
        "symbols": list(symbols),
        "opening_cash": "10000",
        "costs": {
            "fee_bps_per_fill": str(fee_bps_per_fill),
            "slippage_bps_per_side": str(slippage_bps),
            "funding_method": "TIMING_BOUNDS_ENTRY_EXECUTION_NOTIONAL_V1",
        },
        "arms": arms,
        "limitations": [
            "All source dates already inspected; NOT independent OOS evidence",
            "Stop widening is hypothetical; worse realized losses and gaps are possible",
            "Bar-level intrabar ambiguity remains; no order-book or partial-fill simulation",
            "Funding settlement timing is bounded, not account-statement exact",
            "No best variant is chosen; further holdout required",
        ],
    }
    payload["report_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--funding-dir", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--fee-bps-per-fill", type=Decimal, default=Decimal("8"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = run_fixed_exit_study(
        bars_dir=args.bars_dir,
        funding_dir=args.funding_dir,
        symbols=tuple(sorted(args.symbols)),
        fee_bps_per_fill=args.fee_bps_per_fill,
        slippage_bps=args.slippage_bps,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for label, arm in payload["arms"].items():
        print(label, arm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
