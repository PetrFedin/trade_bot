from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioResult,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from tools.qualify_cross_sectional_trading_quality_shadow import (
    _comparison,
    _object,
    _portfolio_policy,
    _position_policy,
    _positive_int,
    _quality_policy,
    _reentry_policy,
    _result_metrics,
    _signal_config,
    _sizing_policy,
    load_config,
    read_csv,
    synchronize_common_timestamps,
)

_SCHEMA = "cross-sectional-trading-quality-walk-forward-v1"
_VARIANTS = ("CONTROL", "SELECTION_ONLY", "SIZING_ONLY", "PROTECTION_ONLY", "COMBINED")


@dataclass(frozen=True)
class WalkForwardPolicy:
    training_bars: int
    holdout_bars: int
    step_bars: int
    minimum_folds: int
    parameter_fitting_allowed: bool
    reset_portfolio_each_fold: bool
    non_overlapping_holdouts: bool

    def validate(self, *, minimum_history_bars: int) -> None:
        if self.training_bars < minimum_history_bars:
            raise ValueError("walk-forward training window is below signal warm-up history")
        if self.holdout_bars < 1:
            raise ValueError("walk-forward holdout_bars must be positive")
        if self.step_bars < 1:
            raise ValueError("walk-forward step_bars must be positive")
        if self.minimum_folds < 1:
            raise ValueError("walk-forward minimum_folds must be positive")
        if self.parameter_fitting_allowed:
            raise ValueError("walk-forward parameter fitting must remain disabled")
        if not self.reset_portfolio_each_fold:
            raise ValueError("walk-forward portfolio must reset each fold")
        if self.non_overlapping_holdouts and self.step_bars < self.holdout_bars:
            raise ValueError("walk-forward holdouts overlap despite non-overlap policy")


@dataclass(frozen=True)
class WalkForwardFold:
    fold_index: int
    training_start_index: int
    holdout_start_index: int
    holdout_end_index_exclusive: int


def _walk_forward_policy(config: dict[str, Any]) -> WalkForwardPolicy:
    raw = _object(config, "walk_forward")
    for field in (
        "parameter_fitting_allowed",
        "reset_portfolio_each_fold",
        "non_overlapping_holdouts",
    ):
        if not isinstance(raw.get(field), bool):
            raise ValueError(f"walk_forward.{field} must be boolean")
    policy = WalkForwardPolicy(
        training_bars=_positive_int(raw, "training_bars"),
        holdout_bars=_positive_int(raw, "holdout_bars"),
        step_bars=_positive_int(raw, "step_bars"),
        minimum_folds=_positive_int(raw, "minimum_folds"),
        parameter_fitting_allowed=raw["parameter_fitting_allowed"],
        reset_portfolio_each_fold=raw["reset_portfolio_each_fold"],
        non_overlapping_holdouts=raw["non_overlapping_holdouts"],
    )
    policy.validate(minimum_history_bars=_signal_config(config).minimum_history_bars)
    return policy


def _folds(timestamp_count: int, policy: WalkForwardPolicy) -> tuple[WalkForwardFold, ...]:
    required = policy.training_bars + policy.holdout_bars
    if timestamp_count < required:
        raise ValueError("insufficient synchronized timestamps for one walk-forward fold")
    folds: list[WalkForwardFold] = []
    start = 0
    while start + required <= timestamp_count:
        holdout_start = start + policy.training_bars
        holdout_end = holdout_start + policy.holdout_bars
        folds.append(
            WalkForwardFold(
                fold_index=len(folds),
                training_start_index=start,
                holdout_start_index=holdout_start,
                holdout_end_index_exclusive=holdout_end,
            )
        )
        start += policy.step_bars
    if len(folds) < policy.minimum_folds:
        raise ValueError("insufficient walk-forward folds for predeclared minimum")
    return tuple(folds)


def _fold_bars(
    bars: tuple[OhlcvBar, ...],
    timeline: tuple,
    fold: WalkForwardFold,
) -> tuple[OhlcvBar, ...]:
    timestamps = set(
        timeline[fold.training_start_index : fold.holdout_end_index_exclusive]
    )
    return tuple(bar for bar in bars if bar.timestamp in timestamps)


def _run_variant(
    *,
    bars: tuple[OhlcvBar, ...],
    config: dict[str, Any],
    first_execution_index: int,
    use_quality_selection: bool,
    use_risk_sizing: bool,
    use_profit_protection: bool,
) -> CrossSectionalPortfolioResult:
    selection = _object(config, "selection")
    selector = CrossSectionalSelector(
        top_k=_positive_int(selection, "top_k"),
        signal_config=_signal_config(config),
        quality_policy=_quality_policy(config) if use_quality_selection else None,
    )
    return CrossSectionalPortfolioBacktester(
        selector=selector,
        portfolio_policy=_portfolio_policy(config),
        position_policy=_position_policy(config, protection=use_profit_protection),
        reentry_policy=_reentry_policy(config),
        sizing_policy=_sizing_policy(config) if use_risk_sizing else None,
    ).run(bars, first_execution_index=first_execution_index)


def _run_variants(
    *,
    bars: tuple[OhlcvBar, ...],
    config: dict[str, Any],
    first_execution_index: int,
) -> dict[str, CrossSectionalPortfolioResult]:
    return {
        "CONTROL": _run_variant(
            bars=bars,
            config=config,
            first_execution_index=first_execution_index,
            use_quality_selection=False,
            use_risk_sizing=False,
            use_profit_protection=False,
        ),
        "SELECTION_ONLY": _run_variant(
            bars=bars,
            config=config,
            first_execution_index=first_execution_index,
            use_quality_selection=True,
            use_risk_sizing=False,
            use_profit_protection=False,
        ),
        "SIZING_ONLY": _run_variant(
            bars=bars,
            config=config,
            first_execution_index=first_execution_index,
            use_quality_selection=False,
            use_risk_sizing=True,
            use_profit_protection=False,
        ),
        "PROTECTION_ONLY": _run_variant(
            bars=bars,
            config=config,
            first_execution_index=first_execution_index,
            use_quality_selection=False,
            use_risk_sizing=False,
            use_profit_protection=True,
        ),
        "COMBINED": _run_variant(
            bars=bars,
            config=config,
            first_execution_index=first_execution_index,
            use_quality_selection=True,
            use_risk_sizing=True,
            use_profit_protection=True,
        ),
    }


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        raise ValueError("walk-forward aggregate cannot average empty values")
    return sum(values, Decimal("0")) / Decimal(len(values))


def qualify(csv_path: Path, config_path: Path) -> dict[str, object]:
    config = load_config(config_path)
    policy = _walk_forward_policy(config)
    raw_bars = read_csv(csv_path)
    bars = synchronize_common_timestamps(
        raw_bars,
        minimum_symbols=_positive_int(config, "minimum_symbols"),
    )
    timeline = tuple(sorted({bar.timestamp for bar in bars}))
    folds = _folds(len(timeline), policy)

    fold_evidence: list[dict[str, object]] = []
    combined_return_deltas: list[Decimal] = []
    combined_drawdown_deltas: list[Decimal] = []
    return_better = 0
    drawdown_not_worse = 0
    both_not_worse = 0

    for fold in folds:
        scoped = _fold_bars(bars, timeline, fold)
        results = _run_variants(
            bars=scoped,
            config=config,
            first_execution_index=policy.training_bars,
        )
        metrics = {name: _result_metrics(result) for name, result in results.items()}
        control = metrics["CONTROL"]
        combined = metrics["COMBINED"]
        comparison = _comparison(combined, control)
        return_delta = Decimal(str(comparison["total_return_delta"]))
        drawdown_delta = Decimal(str(comparison["max_drawdown_fraction_delta"]))
        combined_return_deltas.append(return_delta)
        combined_drawdown_deltas.append(drawdown_delta)
        return_better += return_delta > 0
        drawdown_not_worse += drawdown_delta <= 0
        both_not_worse += return_delta >= 0 and drawdown_delta <= 0

        ablations = {
            name: metrics[name]
            for name in ("SELECTION_ONLY", "SIZING_ONLY", "PROTECTION_ONLY")
        }
        fold_evidence.append(
            {
                "fold_index": fold.fold_index,
                "training_start": timeline[fold.training_start_index].isoformat(),
                "training_end": timeline[fold.holdout_start_index - 1].isoformat(),
                "holdout_start": timeline[fold.holdout_start_index].isoformat(),
                "holdout_end": timeline[
                    fold.holdout_end_index_exclusive - 1
                ].isoformat(),
                "training_bars": policy.training_bars,
                "holdout_bars": policy.holdout_bars,
                "control": control,
                "candidate": combined,
                "ablations": ablations,
                "comparison": comparison,
                "ablation_deltas_vs_control": {
                    name: _comparison(metrics[name], control) for name in ablations
                },
            }
        )

    predeclared_blockers = list(config["promotion_blockers"])
    satisfied = [
        blocker
        for blocker in (
            "NO_WALK_FORWARD_HOLDOUT_EVIDENCE",
            "NO_OUT_OF_SAMPLE_ABLATION_EVIDENCE",
        )
        if blocker in predeclared_blockers
    ]
    remaining = [blocker for blocker in predeclared_blockers if blocker not in satisfied]

    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_WALK_FORWARD_RESEARCH",
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
        "source_csv_sha256": __import__("hashlib").sha256(csv_path.read_bytes()).hexdigest(),
        "symbols": sorted({bar.symbol for bar in bars}),
        "synchronized_timestamp_count": len(timeline),
        "walk_forward_policy": {
            "training_bars": policy.training_bars,
            "holdout_bars": policy.holdout_bars,
            "step_bars": policy.step_bars,
            "minimum_folds": policy.minimum_folds,
            "parameter_fitting_allowed": policy.parameter_fitting_allowed,
            "reset_portfolio_each_fold": policy.reset_portfolio_each_fold,
            "non_overlapping_holdouts": policy.non_overlapping_holdouts,
        },
        "fold_count": len(folds),
        "folds": fold_evidence,
        "aggregate": {
            "mean_combined_total_return_delta": str(_mean(combined_return_deltas)),
            "mean_combined_max_drawdown_fraction_delta": str(
                _mean(combined_drawdown_deltas)
            ),
            "candidate_return_better_fold_count": return_better,
            "candidate_drawdown_not_worse_fold_count": drawdown_not_worse,
            "candidate_return_and_drawdown_not_worse_fold_count": both_not_worse,
        },
        "predeclared_promotion_blockers": predeclared_blockers,
        "evidence_satisfied_blockers": satisfied,
        "remaining_promotion_blockers": remaining,
        "limitations": [
            "PREDECLARED_PARAMETERS_NO_FOLD_FITTING",
            "PORTFOLIO_RESETS_EACH_FOLD",
            "NO_EXTERNAL_PAPER_PORTFOLIO_EXECUTION",
            "DEGRADATION_THRESHOLDS_NOT_CALIBRATED",
            "NO_STRATEGY_PROFITABILITY_CLAIM",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run predeclared rolling walk-forward trading-quality holdouts"
    )
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-config-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    policy = _walk_forward_policy(config)
    if args.validate_config_only:
        print(
            json.dumps(
                {
                    "schema_version": _SCHEMA,
                    "config_valid": True,
                    "parameter_fitting_allowed": policy.parameter_fitting_allowed,
                    "reset_portfolio_each_fold": policy.reset_portfolio_each_fold,
                    "non_overlapping_holdouts": policy.non_overlapping_holdouts,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.csv is None or args.output is None:
        raise ValueError("csv and output are required for walk-forward qualification")
    evidence = qualify(args.csv, args.config)
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    print(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
