from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from app.strategy.correlation_diversification import (
    CorrelationDiversificationPolicy,
    DiversifiedCrossSectionalSelector,
)
from app.strategy.cross_sectional_portfolio import CrossSectionalPortfolioBacktester
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from tools import qualify_correlation_diversification_shadow as diversification
from tools import qualify_cross_sectional_trading_quality_shadow as quality
from tools import qualify_cross_sectional_walk_forward as walk

_SCHEMA = "correlation-diversification-walk-forward-v1"


def _selector(config: dict, *, diversified: bool, policy: dict):
    selection = quality._object(config, "selection")
    base = CrossSectionalSelector(
        top_k=quality._positive_int(selection, "top_k"),
        signal_config=quality._signal_config(config),
        quality_policy=quality._quality_policy(config),
    )
    if not diversified:
        return base
    return DiversifiedCrossSectionalSelector(
        base_selector=base,
        policy=CorrelationDiversificationPolicy(
            lookback_bars=diversification._positive_int(policy, "lookback_bars"),
            minimum_return_observations=diversification._positive_int(
                policy, "minimum_return_observations"
            ),
            maximum_pairwise_correlation=diversification._decimal(
                policy, "maximum_pairwise_correlation"
            ),
        ),
    )


def _run(
    bars,
    *,
    config: dict,
    policy: dict,
    diversified: bool,
    first_execution_index: int,
):
    return CrossSectionalPortfolioBacktester(
        selector=_selector(config, diversified=diversified, policy=policy),
        portfolio_policy=quality._portfolio_policy(config),
        position_policy=quality._position_policy(config, protection=True),
        reentry_policy=quality._reentry_policy(config),
        sizing_policy=quality._sizing_policy(config),
    ).run(bars, first_execution_index=first_execution_index)


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        raise ValueError("diversification walk-forward aggregate is empty")
    return sum(values, Decimal("0")) / Decimal(len(values))


def qualify(
    csv_path: Path,
    trading_quality_config_path: Path,
    diversification_policy_path: Path,
) -> dict[str, object]:
    config = quality.load_config(trading_quality_config_path)
    diversification_policy = diversification.load_policy(diversification_policy_path)
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
    changed_decisions = 0
    better_return_folds = 0
    drawdown_not_worse_folds = 0

    for fold in folds:
        scoped = walk._fold_bars(bars, timeline, fold)
        control = _run(
            scoped,
            config=config,
            policy=diversification_policy,
            diversified=False,
            first_execution_index=walk_policy.training_bars,
        )
        candidate = _run(
            scoped,
            config=config,
            policy=diversification_policy,
            diversified=True,
            first_execution_index=walk_policy.training_bars,
        )
        control_metrics = quality._result_metrics(control)
        candidate_metrics = quality._result_metrics(candidate)
        comparison = quality._comparison(candidate_metrics, control_metrics)
        selection_changes = diversification._selection_change_evidence(
            control.decision_trace,
            candidate.decision_trace,
        )
        return_delta = Decimal(str(comparison["total_return_delta"]))
        drawdown_delta = Decimal(str(comparison["max_drawdown_fraction_delta"]))
        return_deltas.append(return_delta)
        drawdown_deltas.append(drawdown_delta)
        better_return_folds += return_delta > 0
        drawdown_not_worse_folds += drawdown_delta <= 0
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
                "diversified": candidate_metrics,
                "comparison": comparison,
                "selection_changes": selection_changes,
            }
        )

    blockers = list(diversification_policy["promotion_blockers"])
    satisfied = [
        blocker
        for blocker in blockers
        if blocker == "NO_WALK_FORWARD_DIVERSIFICATION_EVIDENCE"
    ]
    remaining = [blocker for blocker in blockers if blocker not in satisfied]

    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_DIVERSIFICATION_WALK_FORWARD_RESEARCH",
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
        "correlation_policy": {
            "lookback_bars": diversification_policy["lookback_bars"],
            "minimum_return_observations": diversification_policy[
                "minimum_return_observations"
            ],
            "maximum_pairwise_correlation": diversification_policy[
                "maximum_pairwise_correlation"
            ],
        },
        "folds": fold_evidence,
        "aggregate": {
            "mean_total_return_delta": str(_mean(return_deltas)),
            "mean_max_drawdown_fraction_delta": str(_mean(drawdown_deltas)),
            "changed_decision_count": changed_decisions,
            "diversified_return_better_fold_count": better_return_folds,
            "diversified_drawdown_not_worse_fold_count": drawdown_not_worse_folds,
        },
        "evidence_satisfied_blockers": satisfied,
        "remaining_promotion_blockers": remaining,
        "limitations": [
            "PREDECLARED_PARAMETERS_NO_FOLD_FITTING",
            "SHORT_CORRELATION_LOOKBACK_FOR_SIGNAL_WARMUP_COMPATIBILITY",
            "CORRELATION_IS_HISTORICAL_NOT_FORWARD_STABLE",
            "NO_EXTERNAL_PAPER_PORTFOLIO_EXECUTION",
            "NO_STRATEGY_PROFITABILITY_CLAIM",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Walk-forward marginal evaluation of correlation diversification"
    )
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--trading-quality-config", type=Path, required=True)
    parser.add_argument("--diversification-policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-config-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = quality.load_config(args.trading_quality_config)
    policy = diversification.load_policy(args.diversification_policy)
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
        raise ValueError("csv and output are required for diversification walk-forward")
    evidence = qualify(
        args.csv,
        args.trading_quality_config,
        args.diversification_policy,
    )
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    print(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
