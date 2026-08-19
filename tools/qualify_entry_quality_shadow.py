from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from app.strategy.cross_sectional_portfolio import CrossSectionalPortfolioBacktester
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.entry_quality import EntryQualityFilteredSelector, EntryQualityPolicy
from tools import qualify_cross_sectional_trading_quality_shadow as quality

_SCHEMA = "entry-quality-filter-shadow-evidence-v1"
_EXPECTED_POLICY_SCHEMA = "entry-quality-filter-shadow-v1"


def load_policy(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != _EXPECTED_POLICY_SCHEMA:
        raise ValueError("unexpected entry-quality policy schema")
    if payload.get("shadow_only") is not True:
        raise ValueError("entry-quality policy must remain shadow_only")
    if payload.get("strategy_promotion_allowed") is not False:
        raise ValueError("entry-quality policy must not allow promotion")
    if not isinstance(payload.get("policy"), dict):
        raise ValueError("entry-quality policy object is required")
    blockers = payload.get("promotion_blockers")
    if not isinstance(blockers, list) or not blockers:
        raise ValueError("entry-quality promotion blockers are required")
    _entry_policy(payload)
    return payload


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"invalid decimal:{field_name}") from exc
    if not parsed.is_finite():
        raise ValueError(f"non-finite decimal:{field_name}")
    return parsed


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid integer:{field_name}")
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{field_name} must be positive")
    return parsed


def _entry_policy(payload: dict) -> EntryQualityPolicy:
    policy = payload["policy"]
    if not isinstance(policy, dict):
        raise ValueError("entry-quality policy object is required")
    liquidity = policy.get("minimum_average_dollar_volume")
    resolved = EntryQualityPolicy(
        lookback_bars=_positive_int(policy.get("lookback_bars"), "lookback_bars"),
        minimum_trend_efficiency=_decimal(
            policy.get("minimum_trend_efficiency"),
            "minimum_trend_efficiency",
        ),
        maximum_price_extension_fraction=_decimal(
            policy.get("maximum_price_extension_fraction"),
            "maximum_price_extension_fraction",
        ),
        maximum_single_bar_return_fraction=_decimal(
            policy.get("maximum_single_bar_return_fraction"),
            "maximum_single_bar_return_fraction",
        ),
        minimum_average_dollar_volume=(
            None
            if liquidity is None
            else _decimal(liquidity, "minimum_average_dollar_volume")
        ),
    )
    resolved.validate()
    return resolved


def _selector(config: dict, *, filtered: bool, policy: dict):
    selection = quality._object(config, "selection")
    base = CrossSectionalSelector(
        top_k=quality._positive_int(selection, "top_k"),
        signal_config=quality._signal_config(config),
        quality_policy=quality._quality_policy(config),
    )
    if not filtered:
        return base
    return EntryQualityFilteredSelector(
        base_selector=base,
        policy=_entry_policy(policy),
    )


def _run(bars, *, config: dict, policy: dict, filtered: bool):
    return CrossSectionalPortfolioBacktester(
        selector=_selector(config, filtered=filtered, policy=policy),
        portfolio_policy=quality._portfolio_policy(config),
        position_policy=quality._position_policy(config, protection=True),
        reentry_policy=quality._reentry_policy(config),
        sizing_policy=quality._sizing_policy(config),
    ).run(bars)


def selection_change_evidence(control_trace, candidate_trace) -> dict[str, object]:
    if len(control_trace) != len(candidate_trace):
        raise ValueError("entry-quality decision traces must have equal length")
    dropped: Counter[str] = Counter()
    added: Counter[str] = Counter()
    changed = 0
    overlap_total = 0
    control_selected_total = 0
    candidate_selected_total = 0
    for control, candidate in zip(control_trace, candidate_trace, strict=True):
        if control.decision_time != candidate.decision_time:
            raise ValueError("entry-quality decision timestamps diverged")
        control_symbols = set(control.selected_symbols)
        candidate_symbols = set(candidate.selected_symbols)
        if control.selected_symbols != candidate.selected_symbols:
            changed += 1
        for symbol in sorted(control_symbols - candidate_symbols):
            dropped[symbol] += 1
        for symbol in sorted(candidate_symbols - control_symbols):
            added[symbol] += 1
        overlap_total += len(control_symbols & candidate_symbols)
        control_selected_total += len(control_symbols)
        candidate_selected_total += len(candidate_symbols)
    decision_count = len(control_trace)
    return {
        "decision_count": decision_count,
        "changed_decision_count": changed,
        "changed_decision_fraction": (
            None if decision_count == 0 else str(Decimal(changed) / Decimal(decision_count))
        ),
        "average_control_selected_count": (
            None
            if decision_count == 0
            else str(Decimal(control_selected_total) / Decimal(decision_count))
        ),
        "average_filtered_selected_count": (
            None
            if decision_count == 0
            else str(Decimal(candidate_selected_total) / Decimal(decision_count))
        ),
        "average_overlap_count": (
            None
            if decision_count == 0
            else str(Decimal(overlap_total) / Decimal(decision_count))
        ),
        "dropped_symbol_counts": dict(sorted(dropped.items())),
        "added_symbol_counts": dict(sorted(added.items())),
    }


def qualify(
    csv_path: Path,
    trading_quality_config_path: Path,
    entry_quality_policy_path: Path,
) -> dict[str, object]:
    config = quality.load_config(trading_quality_config_path)
    policy = load_policy(entry_quality_policy_path)
    raw_bars = quality.read_csv(csv_path)
    bars = quality.synchronize_common_timestamps(
        raw_bars,
        minimum_symbols=quality._positive_int(config, "minimum_symbols"),
    )
    control = _run(bars, config=config, policy=policy, filtered=False)
    candidate = _run(bars, config=config, policy=policy, filtered=True)
    control_metrics = quality._result_metrics(control)
    candidate_metrics = quality._result_metrics(candidate)
    comparison = quality._comparison(candidate_metrics, control_metrics)
    selection_changes = selection_change_evidence(
        control.decision_trace,
        candidate.decision_trace,
    )
    blockers = list(policy["promotion_blockers"])
    satisfied = [
        blocker
        for blocker in blockers
        if blocker == "NO_ENTRY_QUALITY_SAME_SAMPLE_EVIDENCE"
    ]
    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_ENTRY_QUALITY_SHADOW_RESEARCH",
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
        "source_csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "symbols": sorted({bar.symbol for bar in bars}),
        "entry_quality_policy": policy["policy"],
        "control": control_metrics,
        "filtered": candidate_metrics,
        "comparison": comparison,
        "selection_changes": selection_changes,
        "evidence_satisfied_blockers": satisfied,
        "remaining_promotion_blockers": [
            blocker for blocker in blockers if blocker not in satisfied
        ],
        "limitations": list(policy.get("limitations", []))
        + [
            "SAME_SAMPLE_MARGINAL_EVIDENCE_IS_NOT_PROMOTION_EVIDENCE",
            "FILTER_CAN_REDUCE_OPPORTUNITY_COUNT_AS_WELL_AS_BAD_ENTRIES",
            "NO_STRATEGY_PROFITABILITY_CLAIM",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Marginal shadow evaluation of the anti-chase entry-quality filter"
    )
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--trading-quality-config", type=Path, required=True)
    parser.add_argument("--entry-quality-policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-config-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    policy = load_policy(args.entry_quality_policy)
    quality.load_config(args.trading_quality_config)
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
                },
                sort_keys=True,
            )
        )
        return 0
    if args.csv is None or args.output is None:
        raise ValueError("csv and output are required for entry-quality qualification")
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
