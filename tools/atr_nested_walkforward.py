"""Nested chronological ATR profile selection on REUSED historical Bybit data.

No broker access. Outer test windows are later than each inner validation
and do not overlap each other. Only a bounded, preregistered inner matrix
chooses entry-frozen per-coin profiles; nothing is refit on an outer test.
All outcomes remain DEVELOPMENT diagnostics, never untouched OOS evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from tools.atr_entry_frozen_lab import (
    ATR_ARMS,
    AtrEntryProfile,
    FrozenHistoricalSelections,
    fixed_entry_policy,
)
from tools.historical_portfolio_funding import load_funding_schedule
from tools.historical_portfolio_walk_forward import _load_common, folds
from tools.per_asset_exit_policy_matrix import (
    CASH,
    FEE_BPS,
    SIX,
    SLIP_BPS,
    _funding_for_result,
    _row,
)

D = Decimal
OUTER_TRAIN = 420
INNER_VALIDATION = 90
OUTER_TEST = 90
MIN_SYMBOL_CLOSED_TRADES = 3
MIN_LOWER_RETURN_IMPROVEMENT = D("0.005")
MAX_DRAWDOWN_DETERIORATION = D("0.02")
STRESS_FEE_BPS = D("10")
STRESS_SLIPPAGE_BPS = D("10")


def select_past_only_profiles(
    *,
    control: dict,
    inspected_arms: list[dict],
    symbols: tuple[str, ...] = SIX,
) -> tuple[dict[str, str], list[dict]]:
    """Choose from only two preregistered arms per symbol.

    No numerical claim from this helper is an unbiased estimate of
    expected return. In-sample model selection is recorded as such.
    """
    if symbols != SIX:
        raise ValueError("candidate selection requires frozen six symbols")
    expected = {(symbol, name) for symbol in symbols for name in ATR_ARMS}
    observed = [(row["symbol"], row["arm"]) for row in inspected_arms]
    if len(observed) != len(expected) or set(observed) != expected:
        raise ValueError("incomplete or duplicated preregistered inner matrix")
    baseline = D(control["return_with_funding_lower"])
    baseline_dd = D(control["max_drawdown_fraction_before_funding"])
    profiles: dict[str, str] = {}
    evaluations: list[dict] = []
    for symbol in symbols:
        eligible = []
        for item in inspected_arms:
            if item["symbol"] != symbol:
                continue
            delta = D(item["return_with_funding_lower"]) - baseline
            deterioration = (
                D(item["max_drawdown_fraction_before_funding"]) - baseline_dd
            )
            conditions = {
                "sufficient_target_symbol_trades": (
                    item["symbol_closed_trades"] >= MIN_SYMBOL_CLOSED_TRADES
                ),
                "different_target_symbol_trade_path": item["exit_path_changed"],
                "funding_lower_improvement_over_half_percentage_point": (
                    delta > MIN_LOWER_RETURN_IMPROVEMENT
                ),
                "drawdown_not_more_than_two_points_worse": (
                    deterioration <= MAX_DRAWDOWN_DETERIORATION
                ),
            }
            accepted = all(conditions.values())
            evaluations.append({
                "symbol": symbol,
                "arm": item["arm"],
                "eligible": accepted,
                "conditions": conditions,
                "inner_conservative_return_lower": item["return_with_funding_lower"],
                "delta_over_inner_control": str(delta),
                "drawdown_deterioration_fraction": str(deterioration),
                "symbol_closed_trades": item["symbol_closed_trades"],
            })
            if accepted:
                eligible.append((delta, item["arm"]))
        # Reproducible tie-break. BASELINE is the fall-back, not a fitted arm.
        eligible.sort(key=lambda item: (-item[0], item[1]))
        profiles[symbol] = eligible[0][1] if eligible else "BASELINE"
    return profiles, evaluations


def frozen_atr_resolver(
    profiles: dict[str, str],
    *,
    audit: list[dict] | None = None,
):
    if set(profiles) != set(SIX):
        raise ValueError("frozen profile must cover exact six-major universe")
    if any(name not in {*ATR_ARMS, "BASELINE"} for name in profiles.values()):
        raise ValueError("unknown ATR profile cannot be deployed in research")
    fixed = dict(profiles)

    def resolver(symbol, completed):
        if symbol not in fixed:
            raise ValueError("unexpected symbol in ATR resolver")
        if not completed or completed[-1].symbol != symbol:
            raise ValueError("missing symbol-specific completed history")
        selected = fixed[symbol]
        if selected == "BASELINE":
            policy = PositionManagementPolicy()
            atr = None
        else:
            profile = AtrEntryProfile(*ATR_ARMS[selected])
            policy, atr = fixed_entry_policy(
                completed_bars=completed, profile=profile,
            )
        if audit is not None:
            audit.append({
                "symbol": symbol,
                "profile": selected,
                "last_completed_time": completed[-1].timestamp.isoformat(),
                "stop_fraction": str(policy.stop_loss_fraction),
                "target_fraction": str(policy.take_profit_fraction),
                "atr_fraction": None if atr is None else str(atr),
                "frozen_at_entry": True,
            })
        return policy

    return resolver


def _replay(
    *,
    bars, selector, first_execution_index: int,
    resolver=None,
    fee_bps: Decimal = FEE_BPS,
    slippage_bps: Decimal = SLIP_BPS,
):
    engine = CrossSectionalPortfolioBacktester(
        selector=selector,
        portfolio_policy=CrossSectionalPortfolioPolicy(
            opening_cash=CASH,
            fee_per_fill=D("0"),
            fee_bps_per_fill=fee_bps,
            slippage_bps=slippage_bps,
            maximum_gross_exposure_fraction=D("0.60"),
            new_position_target_equity_fraction=D("0.29"),
        ),
        position_policy=PositionManagementPolicy(),
        reentry_policy=ReentryConfirmationPolicy(
            minimum_consecutive_eligible_bars=2,
        ),
        research_entry_policy_resolver=resolver,
    )
    return engine.run(bars, first_execution_index=first_execution_index)


def _window(synchronized, timeline, *, start: int, stop: int):
    times = timeline[start:stop]
    if len(times) != stop - start:
        raise ValueError("out of range synchronized window")
    selected = {
        symbol: synchronized[symbol][start:stop] for symbol in SIX
    }
    if any(
        tuple(bar.timestamp for bar in selected[symbol]) != times
        for symbol in SIX
    ):
        raise ValueError("synchronized window timestamp mismatch")
    return [bar for symbol in SIX for bar in selected[symbol]], selected, times


def _frozen_selector_and_control(bars, *, first_execution_index: int):
    control = _replay(
        bars=bars,
        selector=CrossSectionalSelector(top_k=2),
        first_execution_index=first_execution_index,
    )
    cached = FrozenHistoricalSelections(control.decision_trace, symbols=SIX)
    confirmation = _replay(
        bars=bars, selector=cached, first_execution_index=first_execution_index,
    )
    if confirmation != control:
        raise ValueError("selection cache changes original Strategy A control")
    return cached, control


def _symbol_trade_path(result, symbol):
    return tuple(
        (
            t.entry_time, t.exit_time, t.exit_reason, t.exit_reference_price
        )
        for t in result.closed_trades if t.symbol == symbol
    )


def _metrics(result, schedules, window_sync, window_times):
    return _row(
        result,
        funding=_funding_for_result(
            result, schedules, window_sync, window_times,
        ),
        opening_cash=CASH,
    )


def _benchmark(sync, *, first_exec: int) -> dict[str, str]:
    returns = {
        symbol: (
            bars[-1].close / bars[first_exec].open - D("1")
        )
        for symbol, bars in sync.items()
    }
    return {
        "cash": "0",
        "capital_matched_equal_weight_60pct": str(
            D("0.60") * sum(returns.values(), D("0")) / D(len(returns))
        ),
        "btc_passive_full_capital": str(returns["BTCUSDT"]),
    }


def run_nested_atr(
    *,
    bars_dir: Path,
    funding_dir: Path,
    outer_train: int = OUTER_TRAIN,
    inner_validation: int = INNER_VALIDATION,
    outer_test: int = OUTER_TEST,
    max_folds: int | None = None,
) -> dict:
    if outer_train < 180 or inner_validation < 30 or outer_test < 30:
        raise ValueError("insufficient chronology for nested evaluation")
    if outer_train - inner_validation < 90:
        raise ValueError("insufficient prior inner training context")
    if max_folds is not None and max_folds < 1:
        raise ValueError("max_folds must be positive")
    paths = [bars_dir / f"{symbol}_D.csv" for symbol in SIX]
    funding_paths = [funding_dir / f"{symbol}_funding.csv" for symbol in SIX]
    if any(not path.is_file() for path in [*paths, *funding_paths]):
        raise ValueError("missing pinned OHLCV or funding source")
    loaded, timeline = _load_common(bars_dir, SIX, end_time=None)
    common = set(timeline)
    synchronized = {
        symbol: tuple(bar for bar in loaded[symbol] if bar.timestamp in common)
        for symbol in SIX
    }
    if any(len(synchronized[symbol]) != len(timeline) for symbol in SIX):
        raise ValueError("synchronized window source gaps")
    schedules = {
        symbol: load_funding_schedule(funding_dir / f"{symbol}_funding.csv")
        for symbol in SIX
    }
    plan = folds(
        len(timeline), train_bars=outer_train, test_bars=outer_test,
    )
    if not plan:
        raise ValueError("no nested walk-forward folds")
    if max_folds is not None:
        plan = plan[:max_folds]
    outputs = []
    for fold in plan:
        bars_train, sync_train, times_train = _window(
            synchronized, timeline,
            start=fold.context_start, stop=fold.execution_start,
        )
        inner_start = outer_train - inner_validation
        inner_cached, inner_control = _frozen_selector_and_control(
            bars_train, first_execution_index=inner_start,
        )
        inner_baseline = _metrics(
            inner_control, schedules, sync_train, times_train,
        )
        inner_arms = []
        for symbol in SIX:
            for arm_name in ATR_ARMS:
                policies = {s: "BASELINE" for s in SIX}
                policies[symbol] = arm_name
                result = _replay(
                    bars=bars_train,
                    selector=inner_cached,
                    first_execution_index=inner_start,
                    resolver=frozen_atr_resolver(policies),
                )
                metrics = _metrics(
                    result, schedules, sync_train, times_train,
                )
                inner_arms.append({
                    "symbol": symbol,
                    "arm": arm_name,
                    "symbol_closed_trades": sum(
                        t.symbol == symbol for t in result.closed_trades
                    ),
                    "exit_path_changed": (
                        _symbol_trade_path(result, symbol)
                        != _symbol_trade_path(inner_control, symbol)
                    ),
                    **metrics,
                })
        profiles, decisions = select_past_only_profiles(
            control=inner_baseline, inspected_arms=inner_arms,
        )
        if fold.execution_start != fold.context_start + outer_train:
            raise ValueError("invalid outer fold chronology")
        if times_train[-1] >= timeline[fold.execution_start]:
            raise ValueError("inner training overlaps outer test")
        test_bars, sync_outer, times_outer = _window(
            synchronized, timeline,
            start=fold.context_start, stop=fold.execution_end,
        )
        outer_selector, outer_control = _frozen_selector_and_control(
            test_bars, first_execution_index=outer_train,
        )
        resolver_audit = []
        outer_candidate = _replay(
            bars=test_bars, selector=outer_selector,
            first_execution_index=outer_train,
            resolver=frozen_atr_resolver(profiles, audit=resolver_audit),
        )
        outer_control_metrics = _metrics(
            outer_control, schedules, sync_outer, times_outer,
        )
        outer_candidate_metrics = _metrics(
            outer_candidate, schedules, sync_outer, times_outer,
        )
        stress_policy = {
            "fee_bps_per_fill": str(STRESS_FEE_BPS),
            "slippage_bps_per_side": str(STRESS_SLIPPAGE_BPS),
            "funding_included": False,
            "reason": "Open-position funding bound helper is calibrated to 5bps slippage",
        }
        stress_baseline = _replay(
            bars=test_bars, selector=outer_selector,
            first_execution_index=outer_train,
            fee_bps=STRESS_FEE_BPS,
            slippage_bps=STRESS_SLIPPAGE_BPS,
        )
        stress_candidate = _replay(
            bars=test_bars, selector=outer_selector,
            first_execution_index=outer_train,
            resolver=frozen_atr_resolver(profiles),
            fee_bps=STRESS_FEE_BPS,
            slippage_bps=STRESS_SLIPPAGE_BPS,
        )
        outer_start = timeline[fold.execution_start]
        actual_entries = [
            (symbol, trace.decision_time, trace.execution_time)
            for trace in outer_candidate.decision_trace
            for symbol in trace.entered_symbols
        ]
        if len(actual_entries) != len(resolver_audit):
            raise ValueError("missing or duplicate frozen entry profile audit")
        for (symbol, decision_time, execution_time), entry in zip(
            actual_entries, resolver_audit, strict=True,
        ):
            if (
                entry["symbol"] != symbol
                or entry["last_completed_time"] != decision_time.isoformat()
                or decision_time >= execution_time
            ):
                raise ValueError("entry ATR policy inspected a non-prior bar")
        outputs.append({
            "fold": fold.index,
            "inner_selection": {
                "context_start": times_train[0].isoformat(),
                "validation_start": times_train[inner_start].isoformat(),
                "last_available_decision_bar": times_train[-1].isoformat(),
                "control": inner_baseline,
                "all_predeclared_12_arms": inner_arms,
                "all_selection_reasons": decisions,
                "chosen_profiles_frozen_before_outer_test": profiles,
            },
            "outer_test": {
                "first_execution": outer_start.isoformat(),
                "last_execution": times_outer[-1].isoformat(),
                "execution_bars": outer_test,
                "strategy_a": outer_control_metrics,
                "selected_adaptive_portfolio": outer_candidate_metrics,
                "delta_lower_vs_a": str(
                    D(outer_candidate_metrics["return_with_funding_lower"])
                    - D(outer_control_metrics["return_with_funding_lower"])
                ),
                "selected_profiles_used": profiles,
                "passive_benchmarks": _benchmark(
                    sync_outer, first_exec=outer_train,
                ),
                "entry_policy_audit": resolver_audit,
                "stress_10_10": {
                    "terms": stress_policy,
                    "strategy_a_pre_funding": str(stress_baseline.total_return),
                    "adaptive_pre_funding": str(stress_candidate.total_return),
                    "delta_pre_funding": str(
                        stress_candidate.total_return
                        - stress_baseline.total_return
                    ),
                },
            },
        })
    if any(
        outputs[i]["outer_test"]["first_execution"]
        <= outputs[i - 1]["outer_test"]["last_execution"]
        for i in range(1, len(outputs))
    ):
        raise ValueError("outer test windows overlap")
    lower_adv = [
        D(x["outer_test"]["delta_lower_vs_a"]) for x in outputs
    ]
    adaptive_lower = [
        D(x["outer_test"]["selected_adaptive_portfolio"]["return_with_funding_lower"])
        for x in outputs
    ]
    passive = [
        D(x["outer_test"]["passive_benchmarks"]["capital_matched_equal_weight_60pct"])
        for x in outputs
    ]
    result = {
        "schema_version": "astra-nested-atr-walkforward-development-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORICAL_NESTED_DEVELOPMENT_ONLY",
        "research_only": True,
        "broker_permission": False,
        "live_trading_allowed": False,
        "strategy_promotion_allowed": False,
        "parameter_search_full_sample_permitted": False,
        "untouched_holdout": False,
        "outer_performance_used_for_training": False,
        "optimism_from_multiple_testing_unresolved": True,
        "capital_usd_restarted_each_fold": str(CASH),
        "symbols": list(SIX),
        "configuration": {
            "outer_training_bars": outer_train,
            "inner_validation_bars": inner_validation,
            "outer_test_bars": outer_test,
            "minimum_symbol_trades": MIN_SYMBOL_CLOSED_TRADES,
            "minimum_funding_lower_improvement": str(MIN_LOWER_RETURN_IMPROVEMENT),
            "maximum_drawdown_deterioration": str(MAX_DRAWDOWN_DETERIORATION),
            "experiment_matrix_per_inner_window": len(ATR_ARMS) * len(SIX),
            "fee_bps_per_fill": str(FEE_BPS),
            "slippage_bps_per_side": str(SLIP_BPS),
            "stress_fee_bps": str(STRESS_FEE_BPS),
            "stress_slippage_bps": str(STRESS_SLIPPAGE_BPS),
            "stress_funding_modelled": False,
        },
        "sources_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted([*paths, *funding_paths])
        },
        "summary": {
            "outer_folds": len(outputs),
            "inner_predeclared_arms_evaluated": 12 * len(outputs),
            "outer_adaptive_positive_funding_lower_folds": sum(
                v > 0 for v in adaptive_lower
            ),
            "adaptive_beats_original_lower_folds": sum(
                v > 0 for v in lower_adv
            ),
            "adaptive_beats_passive_capital_matched_lower_folds": sum(
                a > b for a, b in zip(adaptive_lower, passive, strict=True)
            ),
            "sum_restarted_fold_advantages_for_diagnostic_only": str(
                sum(lower_adv, D("0"))
            ),
            "mean_per_fold_lower_advantage": str(
                sum(lower_adv, D("0")) / D(len(outputs))
            ),
            "mean_adaptive_funding_lower": str(
                sum(adaptive_lower, D("0")) / D(len(outputs))
            ),
            "worst_adaptive_funding_lower": str(min(adaptive_lower)),
            "folds_with_no_adaptive_profile_selected": sum(
                all(name == "BASELINE" for name in x["inner_selection"][
                    "chosen_profiles_frozen_before_outer_test"
                ].values())
                for x in outputs
            ),
        },
        "folds": outputs,
        "limitations": [
            "All market dates were previously inspected and are NOT an untouched final holdout",
            "Outer folds restart USD capital; fold returns cannot be compounded as a live account",
            "Selection uses prior inner validation, but profile thresholds were human-designed after earlier research",
            "Existing same-bar stop/target ambiguity is handled conservatively on daily OHLC",
            "Trading fees/slippage are fixed assumed bps; order-book fills and latency unverified",
            "Funding bounds approximate settlement timing and constant entry notional",
            "Stress run explicitly excludes funding, and is not net performance",
            "Policy tuning is preregistered and candidate not altered using outer-test outcomes",
            "A genuinely new future period after final candidate freeze is still required",
        ],
    }
    result["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(result)
    ).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--funding-dir", type=Path, required=True)
    parser.add_argument("--outer-train", type=int, default=OUTER_TRAIN)
    parser.add_argument("--inner-validation", type=int, default=INNER_VALIDATION)
    parser.add_argument("--outer-test", type=int, default=OUTER_TEST)
    parser.add_argument("--max-folds", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_nested_atr(
        bars_dir=args.bars_dir,
        funding_dir=args.funding_dir,
        outer_train=args.outer_train,
        inner_validation=args.inner_validation,
        outer_test=args.outer_test,
        max_folds=args.max_folds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("NESTED_ATR_SUMMARY", json.dumps(report["summary"], sort_keys=True))
    for fold in report["folds"]:
        print("NESTED_ATR_FOLD", fold["fold"], json.dumps({
            "selected": fold["inner_selection"][
                "chosen_profiles_frozen_before_outer_test"
            ],
            "outer_adaptive_lower": fold["outer_test"][
                "selected_adaptive_portfolio"
            ]["return_with_funding_lower"],
            "outer_strategy_a_lower": fold["outer_test"][
                "strategy_a"
            ]["return_with_funding_lower"],
            "delta_lower_vs_a": fold["outer_test"]["delta_lower_vs_a"],
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
