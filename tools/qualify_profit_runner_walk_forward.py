from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from tools import qualify_cross_sectional_trading_quality_shadow as quality
from tools import qualify_cross_sectional_walk_forward as walk
from tools import qualify_profit_runner_shadow as runner

_SCHEMA = "profit-runner-walk-forward-v1"


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        raise ValueError("profit-runner walk-forward aggregate is empty")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _optional_delta(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def qualify(
    csv_path: Path,
    trading_quality_config_path: Path,
    profit_runner_policy_path: Path,
) -> dict[str, object]:
    config = quality.load_config(trading_quality_config_path)
    policy = runner.load_policy(profit_runner_policy_path)
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
    giveback_deltas: list[Decimal] = []
    take_profit_deltas: list[Decimal] = []
    hard_stop_deltas: list[Decimal] = []
    holding_deltas: list[Decimal] = []
    capture_deltas: list[Decimal] = []
    preservation_deltas: list[Decimal] = []
    better_return_folds = 0
    drawdown_not_worse_folds = 0
    capture_not_worse_folds = 0
    hard_stop_not_higher_folds = 0

    for fold in folds:
        scoped = walk._fold_bars(bars, timeline, fold)
        control = runner._run(
            scoped,
            config=config,
            runner=False,
            first_execution_index=walk_policy.training_bars,
        )
        candidate = runner._run(
            scoped,
            config=config,
            runner=True,
            first_execution_index=walk_policy.training_bars,
        )
        control_metrics = runner._extended_metrics(control)
        candidate_metrics = runner._extended_metrics(candidate)
        comparison = runner._comparison(candidate_metrics, control_metrics)

        return_delta = Decimal(str(comparison["total_return_delta"]))
        drawdown_delta = Decimal(str(comparison["max_drawdown_fraction_delta"]))
        turnover_delta = Decimal(str(comparison["turnover_fraction_delta"]))
        take_profit_delta = Decimal(str(comparison["take_profit_exit_count_delta"]))
        hard_stop_delta = Decimal(str(comparison["hard_stop_count_delta"]))
        holding_delta_raw = _optional_delta(
            comparison["average_holding_bars_delta"]
        )
        giveback_delta_raw = _optional_delta(
            comparison["average_mfe_giveback_fraction_delta"]
        )
        capture_delta_raw = _optional_delta(
            comparison["average_mfe_capture_ratio_delta"]
        )
        preservation_delta_raw = _optional_delta(
            comparison["profit_preservation_rate_delta"]
        )

        return_deltas.append(return_delta)
        drawdown_deltas.append(drawdown_delta)
        turnover_deltas.append(turnover_delta)
        take_profit_deltas.append(take_profit_delta)
        hard_stop_deltas.append(hard_stop_delta)
        if holding_delta_raw is not None:
            holding_deltas.append(holding_delta_raw)
        if giveback_delta_raw is not None:
            giveback_deltas.append(giveback_delta_raw)
        if capture_delta_raw is not None:
            capture_deltas.append(capture_delta_raw)
        if preservation_delta_raw is not None:
            preservation_deltas.append(preservation_delta_raw)

        better_return_folds += return_delta > 0
        drawdown_not_worse_folds += drawdown_delta <= 0
        if capture_delta_raw is not None:
            capture_not_worse_folds += capture_delta_raw >= 0
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
                "control_fixed_take_profit": control_metrics,
                "candidate_profit_runner": candidate_metrics,
                "comparison": comparison,
            }
        )

    blockers = list(policy["promotion_blockers"])
    satisfied = [
        blocker
        for blocker in blockers
        if blocker == "NO_PROFIT_RUNNER_WALK_FORWARD_EVIDENCE"
    ]
    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_PROFIT_RUNNER_WALK_FORWARD_RESEARCH",
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
        "profit_runner_policy": policy["policy"],
        "scope": policy["scope"],
        "folds": fold_evidence,
        "aggregate": {
            "mean_total_return_delta": str(_mean(return_deltas)),
            "mean_max_drawdown_fraction_delta": str(_mean(drawdown_deltas)),
            "mean_turnover_fraction_delta": str(_mean(turnover_deltas)),
            "mean_average_mfe_giveback_fraction_delta": (
                None if not giveback_deltas else str(_mean(giveback_deltas))
            ),
            "mean_average_mfe_capture_ratio_delta": (
                None if not capture_deltas else str(_mean(capture_deltas))
            ),
            "mean_profit_preservation_rate_delta": (
                None if not preservation_deltas else str(_mean(preservation_deltas))
            ),
            "mean_take_profit_exit_count_delta": str(_mean(take_profit_deltas)),
            "mean_hard_stop_count_delta": str(_mean(hard_stop_deltas)),
            "mean_average_holding_bars_delta": (
                None if not holding_deltas else str(_mean(holding_deltas))
            ),
            "candidate_return_better_fold_count": better_return_folds,
            "candidate_drawdown_not_worse_fold_count": drawdown_not_worse_folds,
            "candidate_capture_not_worse_observed_fold_count": (
                capture_not_worse_folds
            ),
            "candidate_hard_stop_not_higher_fold_count": (
                hard_stop_not_higher_folds
            ),
        },
        "evidence_satisfied_blockers": satisfied,
        "remaining_promotion_blockers": [
            blocker for blocker in blockers if blocker not in satisfied
        ],
        "limitations": list(policy.get("limitations", []))
        + [
            "PREDECLARED_PARAMETERS_NO_FOLD_FITTING",
            "MISSING_CAPTURE_OR_PRESERVATION_METRICS_ARE_NOT_IMPUTED",
            "RUNNER_MUST_BE_JUDGED_ON_CAPTURE_GIVEBACK_DRAWDOWN_AND_HARD_STOPS_TOGETHER",
            "REAL_PAPER_EXIT_EVIDENCE_STILL_REQUIRED",
            "NO_STRATEGY_PROFITABILITY_CLAIM",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Walk-forward evaluation of fixed take-profit vs profit runner"
    )
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--trading-quality-config", type=Path, required=True)
    parser.add_argument("--profit-runner-policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-config-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = quality.load_config(args.trading_quality_config)
    policy = runner.load_policy(args.profit_runner_policy)
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
        raise ValueError("csv and output are required for profit-runner walk-forward")
    evidence = qualify(
        args.csv,
        args.trading_quality_config,
        args.profit_runner_policy,
    )
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    print(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
