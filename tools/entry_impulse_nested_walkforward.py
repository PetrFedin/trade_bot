"""Past-only nested development test for Candidate B entry impulse veto.

Outer tests are chronologically later, but ALL market history was already
inspected elsewhere. This is pseudo-OOS development, never a final untouched
holdout, live-trading authority, or an empirically tuned winning model.
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
from app.strategy.cross_sectional_selection import (
    CrossSectionalSelection,
    CrossSectionalSelector,
)
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from tools.entry_impulse_exhaustion_lab import (
    CAPITAL,
    FEE_BPS,
    HYPOTHESES,
    SIX,
    SLIPPAGE_BPS,
    _metrics,
    make_prior_impulse_gate,
)
from tools.historical_portfolio_funding import load_funding_schedule
from tools.historical_portfolio_walk_forward import _load_common, folds

D = Decimal
PAST_CONTEXT = 420
INNER_CHECK = 90
LATER_CHECK = 90
MIN_CLOSED_TRADES_TARGET = 2
MIN_TOTAL_TRADES = 8
MIN_LOWER_ADVANTAGE = D("0.005")
MAX_ADDED_DRAWDOWN = D("0.02")
STRESS_FEE_BPS = D("10")
STRESS_SLIPPAGE_BPS = D("10")


class FrozenPastSelector(CrossSectionalSelector):
    """Cache prior-bar ranks from an original control with exact parity proof."""

    def __init__(self, decision_trace, *, symbols=SIX):
        super().__init__(top_k=2)
        self.symbols = tuple(sorted(symbols))
        self.by_time = {
            item.decision_time: (item.execution_index, item.selected_symbols)
            for item in decision_trace
        }
        if len(self.by_time) != len(decision_trace):
            raise ValueError("duplicated original decision timestamps")
        if any(
            len(names) > 2 or len(set(names)) != len(names)
            or set(names) - set(self.symbols)
            for _, names in self.by_time.values()
        ):
            raise ValueError("cached rank names differ from fixed universe")

    def select(self, bars):
        if not bars or bars[-1].symbol != self.symbols[-1]:
            raise ValueError("incomplete sorted prior-bar history in selector")
        timestamp = bars[-1].timestamp
        if timestamp not in self.by_time:
            raise ValueError("decision time not registered in original control")
        execution_index, selected = self.by_time[timestamp]
        if len(bars) != len(self.symbols) * execution_index:
            raise ValueError("original selection context length has changed")
        return CrossSectionalSelection(
            decision_time=timestamp, selected_symbols=selected, candidates=(),
        )


def freeze_past_impulse_map(profiles: dict[str, str], audit=None):
    """One independent frozen profile per coin. Baseline means no veto."""
    if set(profiles) != set(SIX):
        raise ValueError("frozen impulse map has incomplete symbols")
    if any(rule not in {"BASELINE", *HYPOTHESES} for rule in profiles.values()):
        raise ValueError("non-preregistered entry hypothesis")
    copied = dict(profiles)
    by_symbol = {
        symbol: make_prior_impulse_gate(
            targeted_symbols=(symbol,), hypothesis=rule, audit=audit,
        )
        for symbol, rule in copied.items()
        if rule != "BASELINE"
    }

    def gate(symbol, prior_completed_bars):
        if symbol not in copied:
            raise ValueError("unknown entry symbol")
        if copied[symbol] == "BASELINE":
            return True
        return by_symbol[symbol](symbol, prior_completed_bars)

    return gate


def select_profiles_using_inner_only(
    control: dict, arms: list[dict],
) -> tuple[dict[str, str], list[dict]]:
    expected = {(symbol, hypothesis) for symbol in SIX for hypothesis in HYPOTHESES}
    seen = [(item["symbol"], item["hypothesis"]) for item in arms]
    if len(seen) != len(expected) or set(seen) != expected:
        raise ValueError("inner 12-arm matrix missing or duplicated")
    control_lower = D(control["return_funding_lower"])
    control_dd = D(control["max_drawdown_fraction"])
    selected = {}
    reviews = []
    for symbol in SIX:
        eligible = []
        for item in arms:
            if item["symbol"] != symbol:
                continue
            advantage = D(item["return_funding_lower"]) - control_lower
            dd_extra = D(item["max_drawdown_fraction"]) - control_dd
            gates = {
                "actually_vetoed_some_new_entries": item["vetoed_entries"] > 0,
                "at_least_two_target_symbol_closed_trades": (
                    item["target_symbol_closed_trades"] >= MIN_CLOSED_TRADES_TARGET
                ),
                "at_least_eight_total_portfolio_trades": (
                    item["closed_trades"] >= MIN_TOTAL_TRADES
                ),
                "conservative_return_improved_over_half_point": (
                    advantage > MIN_LOWER_ADVANTAGE
                ),
                "drawdown_worsens_no_more_than_two_points": (
                    dd_extra <= MAX_ADDED_DRAWDOWN
                ),
            }
            accepted = all(gates.values())
            reviews.append({
                "symbol": symbol, "hypothesis": item["hypothesis"],
                "eligible": accepted, "checks": gates,
                "past_only_funding_lower_advantage": str(advantage),
                "past_only_added_drawdown": str(dd_extra),
            })
            if accepted:
                eligible.append((advantage, item["hypothesis"]))
        # Objective tie-break, no use of future outer profits.
        eligible.sort(key=lambda row: (-row[0], row[1]))
        selected[symbol] = eligible[0][1] if eligible else "BASELINE"
    return selected, reviews


def _window(sync, timeline, start, end):
    stamps = timeline[start:end]
    if len(stamps) != end - start:
        raise ValueError("window indexes beyond source")
    selected = {symbol: sync[symbol][start:end] for symbol in SIX}
    if any(
        tuple(x.timestamp for x in selected[symbol]) != stamps
        for symbol in SIX
    ):
        raise ValueError("window bars misaligned to common UTC chronology")
    return [x for symbol in SIX for x in selected[symbol]], selected, stamps


def _replay(bars, *, first: int, selector=None, gate=None,
            fee_bps=FEE_BPS, slippage_bps=SLIPPAGE_BPS):
    return CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=2) if selector is None else selector,
        portfolio_policy=CrossSectionalPortfolioPolicy(
            opening_cash=CAPITAL, fee_per_fill=D("0"),
            fee_bps_per_fill=fee_bps, slippage_bps=slippage_bps,
            maximum_gross_exposure_fraction=D("0.60"),
            new_position_target_equity_fraction=D("0.29"),
        ),
        position_policy=PositionManagementPolicy(),
        reentry_policy=ReentryConfirmationPolicy(
            minimum_consecutive_eligible_bars=2,
        ),
        research_prior_entry_veto=gate,
    ).run(bars, first_execution_index=first)


def _baseline_selector(bars, *, first):
    original = _replay(bars, first=first)
    frozen = FrozenPastSelector(original.decision_trace)
    replicated = _replay(bars, first=first, selector=frozen)
    if replicated != original:
        raise ValueError("freezing prior-only selections alters original Strategy A")
    return frozen, original


def _matched_passive(sync, first):
    gains = {
        symbol: prices[-1].close / prices[first].open - D("1")
        for symbol, prices in sync.items()
    }
    return {
        "cash": "0",
        "equal_weight_60pct_exposure": str(
            D("0.60") * sum(gains.values(), D("0")) / D(len(gains))
        ),
        "btc_full_capital_buy_hold": str(gains["BTCUSDT"]),
    }


def run_nested_impulse(
    *,
    bars_dir: Path,
    funding_dir: Path,
    outer_train: int = PAST_CONTEXT,
    inner_test: int = INNER_CHECK,
    outer_test: int = LATER_CHECK,
    max_folds: int | None = None,
) -> dict:
    if outer_train < 180 or inner_test < 30 or outer_test < 30:
        raise ValueError("insufficient source for nested windows")
    if outer_train - inner_test < 90:
        raise ValueError("insufficient prior warmup before inner validation")
    if max_folds is not None and max_folds < 1:
        raise ValueError("max_folds must be positive")
    prices = [bars_dir / f"{symbol}_D.csv" for symbol in SIX]
    funding = [funding_dir / f"{symbol}_funding.csv" for symbol in SIX]
    if any(not path.is_file() for path in (*prices, *funding)):
        raise ValueError("required authentic historical source missing")
    loaded, timeline = _load_common(bars_dir, SIX, end_time=None)
    allowed = set(timeline)
    sync = {
        symbol: tuple(row for row in loaded[symbol] if row.timestamp in allowed)
        for symbol in SIX
    }
    if any(len(sync[symbol]) != len(timeline) for symbol in SIX):
        raise ValueError("non-synchronized original universe")
    schedules = {
        symbol: load_funding_schedule(funding_dir / f"{symbol}_funding.csv")
        for symbol in SIX
    }
    plan = folds(len(timeline), train_bars=outer_train, test_bars=outer_test)
    if not plan:
        raise ValueError("no complete later outer window")
    if max_folds is not None:
        plan = plan[:max_folds]
    outputs = []
    for fold in plan:
        past_bars, past_sync, past_dates = _window(
            sync, timeline, fold.context_start, fold.execution_start,
        )
        inner_start = outer_train - inner_test
        inner_selector, inner_control = _baseline_selector(
            past_bars, first=inner_start,
        )
        inner_control_metrics = _metrics(
            inner_control, schedules, past_sync, past_dates,
        )
        arms = []
        for symbol in SIX:
            for hypothesis in HYPOTHESES:
                audit = []
                chosen = {item: "BASELINE" for item in SIX}
                chosen[symbol] = hypothesis
                experimental = _replay(
                    past_bars, first=inner_start,
                    selector=inner_selector,
                    gate=freeze_past_impulse_map(chosen, audit=audit),
                )
                row = _metrics(experimental, schedules, past_sync, past_dates)
                expected_veto = sum(
                    x["vetoed_new_entry"] for x in audit
                )
                if expected_veto != row["new_entry_veto_count"]:
                    raise ValueError("inner veto rows cannot reconcile")
                arms.append({
                    "symbol": symbol,
                    "hypothesis": hypothesis,
                    "vetoed_entries": expected_veto,
                    "target_symbol_closed_trades": sum(
                        t.symbol == symbol for t in experimental.closed_trades
                    ),
                    "all_inner_veto_attempts": audit,
                    **row,
                })
        profiles, reasons = select_profiles_using_inner_only(
            inner_control_metrics, arms,
        )
        if past_dates[-1] >= timeline[fold.execution_start]:
            raise ValueError("inner validation has future market access")
        outer_bars, outer_sync, outer_dates = _window(
            sync, timeline, fold.context_start, fold.execution_end,
        )
        outer_selector, original = _baseline_selector(
            outer_bars, first=outer_train,
        )
        executed_audit = []
        adaptive = _replay(
            outer_bars, first=outer_train, selector=outer_selector,
            gate=freeze_past_impulse_map(profiles, audit=executed_audit),
        )
        original_metrics = _metrics(original, schedules, outer_sync, outer_dates)
        adaptive_metrics = _metrics(adaptive, schedules, outer_sync, outer_dates)
        if sum(x["vetoed_new_entry"] for x in executed_audit) != (
            adaptive_metrics["new_entry_veto_count"]
        ):
            raise ValueError("outer new-entry veto audit mismatch")
        decision_times = {
            x.decision_time for x in adaptive.decision_trace
        }
        if any(
            x["prior_complete_bar_utc"] not in {
                date.isoformat() for date in decision_times
            }
            for x in executed_audit
        ):
            raise ValueError("outer veto used unknown/nonprior market history")
        if any(
            date >= original.decision_trace[-1].execution_time.isoformat()
            for date in (x["prior_complete_bar_utc"] for x in executed_audit)
        ):
            raise ValueError("future candle leaked into entry callback")
        stress_base = _replay(
            outer_bars, first=outer_train, selector=outer_selector,
            fee_bps=STRESS_FEE_BPS, slippage_bps=STRESS_SLIPPAGE_BPS,
        )
        stress_adaptive = _replay(
            outer_bars, first=outer_train, selector=outer_selector,
            gate=freeze_past_impulse_map(profiles),
            fee_bps=STRESS_FEE_BPS, slippage_bps=STRESS_SLIPPAGE_BPS,
        )
        outputs.append({
            "fold": fold.index,
            "inner_training": {
                "first_past_bar": past_dates[0].isoformat(),
                "inner_test_start": past_dates[inner_start].isoformat(),
                "last_known_prior_bar": past_dates[-1].isoformat(),
                "unchanged_strategy_a": inner_control_metrics,
                "all_12_single_symbol_arms": arms,
                "all_rejections_and_qualifications": reasons,
                "selected_profile_map_before_outer_test": profiles,
            },
            "outer_test": {
                "first_execution_bar": timeline[fold.execution_start].isoformat(),
                "last_execution_bar": outer_dates[-1].isoformat(),
                "original_strategy_a": original_metrics,
                "prior_picked_joint_entry_policy": adaptive_metrics,
                "conservative_lower_advantage": str(
                    D(adaptive_metrics["return_funding_lower"])
                    - D(original_metrics["return_funding_lower"])
                ),
                "selected_profiles": dict(profiles),
                "all_entry_gate_attempts": executed_audit,
                "benchmark_returns_before_costs": _matched_passive(
                    outer_sync, first=outer_train,
                ),
                "stress_10bps_fee_10bps_slippage_pre_funding": {
                    "funding_included": False,
                    "original": str(stress_base.total_return),
                    "adaptive": str(stress_adaptive.total_return),
                    "delta": str(
                        stress_adaptive.total_return - stress_base.total_return
                    ),
                },
            },
        })
    if any(
        outputs[i - 1]["outer_test"]["last_execution_bar"]
        >= outputs[i]["outer_test"]["first_execution_bar"]
        for i in range(1, len(outputs))
    ):
        raise ValueError("outer test windows are not strictly disjoint")
    differences = [
        D(f["outer_test"]["conservative_lower_advantage"]) for f in outputs
    ]
    adaptive_returns = [
        D(f["outer_test"]["prior_picked_joint_entry_policy"]["return_funding_lower"])
        for f in outputs
    ]
    passive = [
        D(f["outer_test"]["benchmark_returns_before_costs"]["equal_weight_60pct_exposure"])
        for f in outputs
    ]
    result = {
        "schema_version": "astra-candidate-b-nested-entry-veto-development-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORICAL_NESTED_ENTRY_DEVELOPMENT",
        "research_only": True,
        "live_trading_allowed": False,
        "strategy_promotion_allowed": False,
        "untouched_holdout": False,
        "outer_test_used_to_choose_profiles": False,
        "predeclared_inner_hypotheses": list(HYPOTHESES),
        "origin_data_was_previously_inspected": True,
        "capital_restarts_per_fold_usd": str(CAPITAL),
        "original_exit_policy_unchanged": True,
        "all_negative_arms_kept": True,
        "configuration": {
            "outer_training_daily_bars": outer_train,
            "inner_validation_daily_bars": inner_test,
            "outer_test_daily_bars": outer_test,
            "max_symbol_profiles": 2,
            "min_closed_trades_target": MIN_CLOSED_TRADES_TARGET,
            "min_total_trades": MIN_TOTAL_TRADES,
            "min_funding_lower_advantage": str(MIN_LOWER_ADVANTAGE),
            "max_added_drawdown_fraction": str(MAX_ADDED_DRAWDOWN),
            "fee_bps_per_fill": str(FEE_BPS),
            "slippage_bps_per_side": str(SLIPPAGE_BPS),
            "stress_fee_bps": str(STRESS_FEE_BPS),
            "stress_slippage_bps_per_side": str(STRESS_SLIPPAGE_BPS),
            "stress_includes_funding": False,
        },
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((*prices, *funding))
        },
        "summary": {
            "outer_test_folds": len(outputs),
            "inner_candidate_portfolios": len(outputs) * 12,
            "adaptive_better_folds": sum(x > 0 for x in differences),
            "adaptive_equal_folds": sum(x == 0 for x in differences),
            "adaptive_worse_folds": sum(x < 0 for x in differences),
            "mean_lower_advantage_per_restarted_fold": str(
                sum(differences, D("0")) / D(len(differences))
            ),
            "mean_adaptive_lower_return": str(
                sum(adaptive_returns, D("0")) / D(len(adaptive_returns))
            ),
            "worst_adaptive_lower_return": str(min(adaptive_returns)),
            "positive_adaptive_lower_folds": sum(
                value > 0 for value in adaptive_returns
            ),
            "beat_passive_capital_matched_lower_folds": sum(
                a > b for a, b in zip(adaptive_returns, passive, strict=True)
            ),
            "all_baseline_profile_folds": sum(
                all(name == "BASELINE" for name in fold["inner_training"][
                    "selected_profile_map_before_outer_test"
                ].values())
                for fold in outputs
            ),
        },
        "folds": outputs,
        "limitations": [
            "Each market date was previously inspected; nested folds are",
            "not a new untouched external future holdout",
            "All fold capital begins at $10k; returns must not be compounded as one account",
            "Human-proposed impulse thresholds came after prior Strategy A loss analysis",
            "Funding cost bounds reflect constant entry notional and uncertain timing",
            "Daily stop and target collision order is not exact exchange execution",
            "Slippage and fees assumed fixed, orderbook and partial fills unknown",
            "The 10/10 cost stress is before funding and not directly comparable to lower bounds",
            "No automatically promoted or real-money trading policy",
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
    parser.add_argument("--outer-train", type=int, default=PAST_CONTEXT)
    parser.add_argument("--inner-validation", type=int, default=INNER_CHECK)
    parser.add_argument("--outer-test", type=int, default=LATER_CHECK)
    parser.add_argument("--max-folds", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_nested_impulse(
        bars_dir=args.bars_dir,
        funding_dir=args.funding_dir,
        outer_train=args.outer_train,
        inner_test=args.inner_validation,
        outer_test=args.outer_test,
        max_folds=args.max_folds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print("NESTED_IMPULSE_SUMMARY", json.dumps(report["summary"], sort_keys=True))
    for fold in report["folds"]:
        print("NESTED_IMPULSE_FOLD", fold["fold"], json.dumps({
            "profile_map": fold["inner_training"][
                "selected_profile_map_before_outer_test"
            ],
            "strategy_a_lower": fold["outer_test"][
                "original_strategy_a"
            ]["return_funding_lower"],
            "candidate_lower": fold["outer_test"][
                "prior_picked_joint_entry_policy"
            ]["return_funding_lower"],
            "delta_lower": fold["outer_test"]["conservative_lower_advantage"],
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
