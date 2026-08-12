from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from app.strategy.cross_sectional_portfolio import CrossSectionalPortfolioBacktester
from tools import qualify_cross_sectional_trading_quality_shadow as quality
from tools import qualify_cross_sectional_walk_forward as walk
from tools import qualify_entry_quality_shadow as entry

_SCHEMA = "entry-quality-filter-walk-forward-v1"


def _run(
    bars,
    *,
    config: dict,
    policy: dict,
    filtered: bool,
    first_execution_index: int,
):
    return CrossSectionalPortfolioBacktester(
        selector=entry._selector(config, filtered=filtered, policy=policy),
        portfolio_policy=quality._portfolio_policy(config),
        position_policy=quality._position_policy(config, protection=True),
        reentry_policy=quality._reentry_policy(config),
        sizing_policy=quality._sizing_policy(config),
    ).run(bars, first_execution_index=first_execution_index)


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        raise ValueError("entry-quality walk-forward aggregate is empty")
    return sum(values, Decimal("0")) / Decimal(len(values))


def qualify(
    csv_path: Path,
    trading_quality_config_path: Path,
    entry_quality_policy_path: Path,
) -> dict[str, object]:
    config = quality.load_config(trading_quality_config_path)
    policy = entry.load_policy(entry_quality_policy_path)
    walk_policy = walk._walk_forward_policy(config)
    raw_bars = quality.read_csv(csv_path)
    bars = quality.synchronize_common_timestamps(
        raw_bars,
        minimum_symbols=quality._positive_int(config, "minimum_symbols"),
    )
    timeline = tuple(sorted({bar.timestamp for bar in bars}))
    folds = walk._folds(len(timeline), walk_policy)

    fold_evidence: list[dict[str, object]] = []
    return_deltas: list[Decimal] = []
    drawdown_deltas: list[Decimal] = []
    trade_count_deltas: list[Decimal] = []
    changed_decisions = 0
    better_return_folds = 0
    drawdown_not_worse_folds = 0
    lower_or_equal_turnover_folds = 0

    for fold in folds:
        scoped = walk._fold_bars(bars, timeline, fold)
        control = _run(
            scoped,
            config=config,
            policy=policy,
            filtered=False,
            first_execution_index=walk_policy.training_bars,
        )
        candidate = _run(
            scoped,
            config=config,
            policy=policy,
            filtered=True,
            first_execution_index=walk_policy.training_bars,
        )
        control_metrics = quality._result_metrics(control)
        candidate_metrics = quality._result_metrics(candidate)
        comparison = quality._comparison(candidate_metrics, control_metrics)
        selection_changes = entry.selection_change_evidence(
            control.decision_trace,
            candidate.decision_trace,
        )
        return_delta = Decimal(str(comparison["total_return_delta"]))
        drawdown_delta = Decimal(str(comparison["max_drawdown_fraction_delta"]))
        trade_count_delta = Decimal(
            str(candidate_metrics["trade_count"])
        ) - Decimal(str(control_metrics["trade_count"]))
        return_deltas.append(return_delta)
        drawdown_deltas.append(drawdown_delta)
        trade_count_deltas.append(trade_count_delta)
        better_return_folds += return_delta > 0
        drawdown_not_worse_folds += drawdown_delta <= 0
        lower_or_equal_turnover_folds += Decimal(
            str(candidate_metrics["turnover_notional"])
        ) <= Decimal(str(control_metrics["turnover_notional"]))
        changed_decisions += int(selection_changes["changed_decision_count"])

        fold_evidence.append(
            {
                "fold_index": fold.fold_index,
                "training_start": timeline[fold.training_start_index].isoformat(),
                "training_end": timeline[fold.holdout_start_index - 1].isoformat(),
                "holdout_start": timeline[fold.holdout_start_index].isoformat(),
                "holdout_end": timeline[
                    fold.holdout_end_index_exclusive - 1
                ].isoformat(),
                "control": control_metrics,
                "filtered": candidate_metrics,
                "comparison": comparison,
                "selection_changes": selection_changes,
                "trade_count_delta": str(trade_count_delta),
            }
        )

    blockers = list(policy["promotion_blockers"])
    satisfied = [
        blocker
        for blocker in blockers
        if blocker == "NO_ENTRY_QUALITY_WALK_FORWARD_EVIDENCE"
    ]
    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_ENTRY_QUALITY_WALK_FORWARD_RESEARCH",
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
        "source_csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "symbols": sorted({bar.symbol for bar in bars}),
        "fold_count": len(folds),
        "walk_forward_policy": {
            "training_bars": walk_policy.training_bars,
            "holdout_bars": walk_policy.holdout_bars,
            "step_bars": walk_policy.step_bars,
            "parameter_fitting_allowed": walk_policy.parameter_fitting_allowed,
            "reset_portfolio_each_fold": walk_policy.reset_portfolio_each_fold,
            "non_overlapping_holdouts": walk_policy.non_overlapping_holdouts,
        },
        "entry_quality_policy": policy["policy"],
        "folds": fold_evidence,
        "aggregate": {
            "mean_total_return_delta": str(_mean(return_deltas)),
            "mean_max_drawdown_fraction_delta": str(_mean(drawdown_deltas)),
            "mean_trade_count_delta": str(_mean(trade_count_deltas)),
            "changed_decision_count": changed_decisions,
            "filtered_return_better_fold_count": better_return_folds,
            "filtered_drawdown_not_worse_fold_count": drawdown_not_worse_folds,
            "filtered_turnover_lower_or_equal_fold_count": (
                lower_or_equal_turnover_folds
            ),
        },
        "evidence_satisfied_blockers": satisfied,
        "remaining_promotion_blockers": [
            blocker for blocker in blockers if blocker not in satisfied
        ],
        "limitations": list(policy.get("limitations", []))
        + [
            "PREDECLARED_PARAMETERS_NO_FOLD_FITTING",
            "FILTER_MAY_IMPROVE_RISK_BY_REDUCING_ACTIVITY_RATHER_THAN_SELECTION_EDGE",
            "REAL_PAPER_SPREAD_DEPTH_AND_FILL_EVIDENCE_STILL_REQUIRED",
            "NO_STRATEGY_PROFITABILITY_CLAIM",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Walk-forward marginal evaluation of anti-chase entry quality"
    )
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--trading-quality-config", type=Path, required=True)
    parser.add_argument("--entry-quality-policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-config-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = quality.load_config(args.trading_quality_config)
    policy = entry.load_policy(args.entry_quality_policy)
    walk_policy = walk._walk_forward_policy(config)
    if args.validate_config_only:
        print(
            json.dumps(
                {
                    "schema_version": _SCHEMA,
                    "config_valid": True,
                    "shadow_only": policy["shadow_only"],
                    "strategy_promotion_allowed": policy[
                        "strategy_promotion_allowed"
                    ],
                    "parameter_fitting_allowed": (
                        walk_policy.parameter_fitting_allowed
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.csv is None or args.output is None:
        raise ValueError("csv and output are required for entry-quality walk-forward")
    evidence = qualify(
        args.csv,
        args.trading_quality_config,
        args.entry_quality_policy,
    )
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    print(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
