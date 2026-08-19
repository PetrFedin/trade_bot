from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from tools import qualify_cross_sectional_trading_quality_shadow as quality
from tools import qualify_cross_sectional_walk_forward as walk
from tools import qualify_selection_exit_confirmation_shadow as selection_exit

_SCHEMA = "selection-exit-confirmation-walk-forward-v1"


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        raise ValueError("selection-exit walk-forward aggregate is empty")
    return sum(values, Decimal("0")) / Decimal(len(values))


def qualify(
    csv_path: Path,
    trading_quality_config_path: Path,
    selection_exit_policy_path: Path,
) -> dict[str, object]:
    config = quality.load_config(trading_quality_config_path)
    policy = selection_exit.load_policy(selection_exit_policy_path)
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
    turnover_deltas: list[Decimal] = []
    selection_exit_deltas: list[Decimal] = []
    hard_stop_deltas: list[Decimal] = []
    holding_deltas: list[Decimal] = []
    pending_count = 0
    better_return_folds = 0
    drawdown_not_worse_folds = 0
    hard_stop_not_higher_folds = 0

    for fold in folds:
        scoped = walk._fold_bars(bars, timeline, fold)
        control = selection_exit._run(
            scoped,
            config=config,
            policy=policy,
            confirmed=False,
            first_execution_index=walk_policy.training_bars,
        )
        candidate = selection_exit._run(
            scoped,
            config=config,
            policy=policy,
            confirmed=True,
            first_execution_index=walk_policy.training_bars,
        )
        control_metrics = selection_exit._extended_metrics(control)
        candidate_metrics = selection_exit._extended_metrics(candidate)
        comparison = selection_exit._comparison(candidate_metrics, control_metrics)
        return_delta = Decimal(str(comparison["total_return_delta"]))
        drawdown_delta = Decimal(str(comparison["max_drawdown_fraction_delta"]))
        turnover_delta = Decimal(str(comparison["turnover_fraction_delta"]))
        selection_exit_delta = Decimal(str(comparison["selection_exit_count_delta"]))
        hard_stop_delta = Decimal(str(comparison["hard_stop_count_delta"]))
        holding_delta_raw = comparison["average_holding_bars_delta"]
        holding_delta = (
            Decimal("0")
            if holding_delta_raw is None
            else Decimal(str(holding_delta_raw))
        )
        return_deltas.append(return_delta)
        drawdown_deltas.append(drawdown_delta)
        turnover_deltas.append(turnover_delta)
        selection_exit_deltas.append(selection_exit_delta)
        hard_stop_deltas.append(hard_stop_delta)
        holding_deltas.append(holding_delta)
        pending_count += int(
            candidate_metrics["selection_exit_confirmation_pending_count"]
        )
        better_return_folds += return_delta > 0
        drawdown_not_worse_folds += drawdown_delta <= 0
        hard_stop_not_higher_folds += hard_stop_delta <= 0
        fold_evidence.append(
            {
                "fold_index": fold.fold_index,
                "training_start": timeline[fold.training_start_index].isoformat(),
                "training_end": timeline[fold.holdout_start_index - 1].isoformat(),
                "holdout_start": timeline[fold.holdout_start_index].isoformat(),
                "holdout_end": timeline[
                    fold.holdout_end_index_exclusive - 1
                ].isoformat(),
                "control_immediate_exit": control_metrics,
                "candidate_confirmed_exit": candidate_metrics,
                "comparison": comparison,
            }
        )

    blockers = list(policy["promotion_blockers"])
    satisfied = [
        blocker
        for blocker in blockers
        if blocker == "NO_SELECTION_EXIT_WALK_FORWARD_EVIDENCE"
    ]
    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_SELECTION_EXIT_CONFIRMATION_WALK_FORWARD_RESEARCH",
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
        "selection_exit_policy": policy["policy"],
        "scope": policy["scope"],
        "folds": fold_evidence,
        "aggregate": {
            "mean_total_return_delta": str(_mean(return_deltas)),
            "mean_max_drawdown_fraction_delta": str(_mean(drawdown_deltas)),
            "mean_turnover_fraction_delta": str(_mean(turnover_deltas)),
            "mean_selection_exit_count_delta": str(_mean(selection_exit_deltas)),
            "mean_hard_stop_count_delta": str(_mean(hard_stop_deltas)),
            "mean_average_holding_bars_delta": str(_mean(holding_deltas)),
            "pending_confirmation_count": pending_count,
            "candidate_return_better_fold_count": better_return_folds,
            "candidate_drawdown_not_worse_fold_count": drawdown_not_worse_folds,
            "candidate_hard_stop_not_higher_fold_count": hard_stop_not_higher_folds,
        },
        "evidence_satisfied_blockers": satisfied,
        "remaining_promotion_blockers": [
            blocker for blocker in blockers if blocker not in satisfied
        ],
        "limitations": list(policy.get("limitations", []))
        + [
            "PREDECLARED_PARAMETERS_NO_FOLD_FITTING",
            "LOWER_SELECTION_EXIT_COUNT_IS_NOT_BY_ITSELF_AN_IMPROVEMENT",
            "HARD_STOP_AND_DRAWDOWN_DELTAS_MUST_BE_REVIEWED_TOGETHER",
            "REAL_PAPER_DECISION_EVIDENCE_STILL_REQUIRED",
            "NO_STRATEGY_PROFITABILITY_CLAIM",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Walk-forward evaluation of shadow selection-exit confirmation"
    )
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--trading-quality-config", type=Path, required=True)
    parser.add_argument("--selection-exit-policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-config-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = quality.load_config(args.trading_quality_config)
    policy = selection_exit.load_policy(args.selection_exit_policy)
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
                    "minimum_symbols": config["minimum_symbols"],
                    "parameter_fitting_allowed": (
                        walk_policy.parameter_fitting_allowed
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.csv is None or args.output is None:
        raise ValueError("csv and output are required for selection-exit walk-forward")
    evidence = qualify(
        args.csv,
        args.trading_quality_config,
        args.selection_exit_policy,
    )
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    print(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
