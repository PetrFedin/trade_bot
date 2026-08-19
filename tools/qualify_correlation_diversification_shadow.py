from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.strategy.correlation_diversification import (
    CorrelationDiversificationPolicy,
    DiversifiedCrossSectionalSelector,
)
from app.strategy.cross_sectional_portfolio import CrossSectionalPortfolioBacktester
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from tools import qualify_cross_sectional_trading_quality_shadow as quality

_SCHEMA = "correlation-diversification-shadow-v1"


def _positive_int(data: dict[str, Any], field: str) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _decimal(data: dict[str, Any], field: str) -> Decimal:
    value = data.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal string")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def load_policy(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
        raise ValueError("correlation diversification policy schema mismatch")
    if data.get("shadow_only") is not True:
        raise ValueError("correlation diversification must remain shadow-only")
    if data.get("strategy_promotion_allowed") is not False:
        raise ValueError("correlation diversification cannot allow strategy promotion")
    if data.get("comparison_basis") != "COMBINED_V2_WITHOUT_VS_WITH_DIVERSIFICATION":
        raise ValueError("correlation diversification comparison basis changed")
    policy = CorrelationDiversificationPolicy(
        lookback_bars=_positive_int(data, "lookback_bars"),
        minimum_return_observations=_positive_int(
            data, "minimum_return_observations"
        ),
        maximum_pairwise_correlation=_decimal(
            data, "maximum_pairwise_correlation"
        ),
    )
    policy.validate()
    for field in ("limitations", "promotion_blockers"):
        value = data.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError(f"{field} must be a list of strings")
    return data


def _policy(data: dict[str, Any]) -> CorrelationDiversificationPolicy:
    return CorrelationDiversificationPolicy(
        lookback_bars=_positive_int(data, "lookback_bars"),
        minimum_return_observations=_positive_int(
            data, "minimum_return_observations"
        ),
        maximum_pairwise_correlation=_decimal(
            data, "maximum_pairwise_correlation"
        ),
    )


def _base_selector(config: dict[str, Any]) -> CrossSectionalSelector:
    selection = quality._object(config, "selection")
    return CrossSectionalSelector(
        top_k=quality._positive_int(selection, "top_k"),
        signal_config=quality._signal_config(config),
        quality_policy=quality._quality_policy(config),
    )


def _backtester(selector: object, config: dict[str, Any]) -> CrossSectionalPortfolioBacktester:
    return CrossSectionalPortfolioBacktester(
        selector=selector,  # type: ignore[arg-type]
        portfolio_policy=quality._portfolio_policy(config),
        position_policy=quality._position_policy(config, protection=True),
        reentry_policy=quality._reentry_policy(config),
        sizing_policy=quality._sizing_policy(config),
    )


def _selection_change_evidence(
    control_trace: tuple,
    diversified_trace: tuple,
) -> dict[str, object]:
    if len(control_trace) != len(diversified_trace):
        raise RuntimeError("diversification comparison trace length drifted")
    changed = 0
    overlap_sum = Decimal("0")
    dropped: Counter[str] = Counter()
    added: Counter[str] = Counter()
    control_selected_total = 0
    diversified_selected_total = 0

    for control, diversified in zip(control_trace, diversified_trace, strict=True):
        if control.execution_index != diversified.execution_index:
            raise RuntimeError("diversification comparison execution index drifted")
        control_set = set(control.selected_symbols)
        diversified_set = set(diversified.selected_symbols)
        control_selected_total += len(control_set)
        diversified_selected_total += len(diversified_set)
        denominator = max(1, len(control_set))
        overlap_sum += Decimal(len(control_set & diversified_set)) / Decimal(denominator)
        if control_set != diversified_set:
            changed += 1
            dropped.update(control_set - diversified_set)
            added.update(diversified_set - control_set)

    count = len(control_trace)
    return {
        "decision_count": count,
        "changed_decision_count": changed,
        "changed_decision_fraction": (
            str(Decimal(changed) / Decimal(count)) if count else "0"
        ),
        "average_selected_overlap_fraction": (
            str(overlap_sum / Decimal(count)) if count else "0"
        ),
        "average_selected_count_control": (
            str(Decimal(control_selected_total) / Decimal(count)) if count else "0"
        ),
        "average_selected_count_diversified": (
            str(Decimal(diversified_selected_total) / Decimal(count))
            if count
            else "0"
        ),
        "dropped_symbol_counts": dict(sorted(dropped.items())),
        "added_symbol_counts": dict(sorted(added.items())),
    }


def qualify(
    csv_path: Path,
    trading_quality_config_path: Path,
    diversification_policy_path: Path,
) -> dict[str, object]:
    config = quality.load_config(trading_quality_config_path)
    diversification_data = load_policy(diversification_policy_path)
    raw_bars = quality.read_csv(csv_path)
    bars = quality.synchronize_common_timestamps(
        raw_bars,
        minimum_symbols=quality._positive_int(config, "minimum_symbols"),
    )

    base_selector = _base_selector(config)
    control = _backtester(base_selector, config).run(bars)
    diversified_selector = DiversifiedCrossSectionalSelector(
        base_selector=_base_selector(config),
        policy=_policy(diversification_data),
    )
    diversified = _backtester(diversified_selector, config).run(bars)

    control_metrics = quality._result_metrics(control)
    diversified_metrics = quality._result_metrics(diversified)
    selection_changes = _selection_change_evidence(
        control.decision_trace,
        diversified.decision_trace,
    )

    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_DIVERSIFICATION_RESEARCH",
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
        "source_csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "symbols": sorted({bar.symbol for bar in bars}),
        "correlation_policy": {
            "lookback_bars": diversification_data["lookback_bars"],
            "minimum_return_observations": diversification_data[
                "minimum_return_observations"
            ],
            "maximum_pairwise_correlation": diversification_data[
                "maximum_pairwise_correlation"
            ],
        },
        "control": control_metrics,
        "diversified": diversified_metrics,
        "comparison": quality._comparison(diversified_metrics, control_metrics),
        "selection_changes": selection_changes,
        "limitations": list(diversification_data["limitations"]),
        "promotion_blockers": list(diversification_data["promotion_blockers"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure correlation diversification as a marginal shadow component"
    )
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--trading-quality-config", type=Path, required=True)
    parser.add_argument("--diversification-policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-config-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    quality.load_config(args.trading_quality_config)
    policy = load_policy(args.diversification_policy)
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
        raise ValueError("csv and output are required for diversification qualification")
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
