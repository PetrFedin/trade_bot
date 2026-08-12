from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioResult,
    PortfolioExitReason,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.selection_exit_confirmation import SelectionExitConfirmationPolicy
from tools import qualify_cross_sectional_trading_quality_shadow as quality

_SCHEMA = "selection-exit-confirmation-shadow-evidence-v1"
_EXPECTED_POLICY_SCHEMA = "selection-exit-confirmation-shadow-v1"


def load_policy(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != _EXPECTED_POLICY_SCHEMA:
        raise ValueError("unexpected selection-exit policy schema")
    if payload.get("shadow_only") is not True:
        raise ValueError("selection-exit policy must remain shadow_only")
    if payload.get("strategy_promotion_allowed") is not False:
        raise ValueError("selection-exit policy must not allow promotion")
    if not isinstance(payload.get("policy"), dict):
        raise ValueError("selection-exit policy object is required")
    if not isinstance(payload.get("promotion_blockers"), list):
        raise ValueError("selection-exit promotion blockers are required")
    _selection_exit_policy(payload)
    return payload


def _selection_exit_policy(payload: dict) -> SelectionExitConfirmationPolicy:
    policy = payload["policy"]
    resolved = SelectionExitConfirmationPolicy(
        minimum_consecutive_deselected_bars=int(
            policy["minimum_consecutive_deselected_bars"]
        ),
        exit_profitable_positions_immediately=bool(
            policy["exit_profitable_positions_immediately"]
        ),
        reset_on_reselection=bool(policy["reset_on_reselection"]),
    )
    resolved.validate()
    return resolved


def _selector(config: dict) -> CrossSectionalSelector:
    selection = quality._object(config, "selection")
    return CrossSectionalSelector(
        top_k=quality._positive_int(selection, "top_k"),
        signal_config=quality._signal_config(config),
        quality_policy=quality._quality_policy(config),
    )


def _run(
    bars,
    *,
    config: dict,
    policy: dict,
    confirmed: bool,
    first_execution_index: int | None = None,
) -> CrossSectionalPortfolioResult:
    backtester = CrossSectionalPortfolioBacktester(
        selector=_selector(config),
        portfolio_policy=quality._portfolio_policy(config),
        position_policy=quality._position_policy(config, protection=True),
        reentry_policy=quality._reentry_policy(config),
        sizing_policy=quality._sizing_policy(config),
        selection_exit_policy=(
            _selection_exit_policy(policy) if confirmed else None
        ),
    )
    return backtester.run(bars, first_execution_index=first_execution_index)


def _extended_metrics(result: CrossSectionalPortfolioResult) -> dict[str, object]:
    metrics = dict(quality._result_metrics(result))
    reason_counts = Counter(trade.exit_reason.value for trade in result.closed_trades)
    holding_bars = tuple(trade.holding_bars for trade in result.closed_trades)
    metrics.update(
        {
            "selection_exit_confirmation_pending_count": (
                result.selection_exit_confirmation_pending_count
            ),
            "selection_exit_count": reason_counts[PortfolioExitReason.SELECTION_EXIT.value],
            "hard_stop_count": reason_counts[
                PortfolioExitReason.INTRABAR_HARD_STOP.value
            ],
            "profit_protection_exit_count": reason_counts[
                PortfolioExitReason.INTRABAR_PROFIT_PROTECTION.value
            ],
            "average_holding_bars": (
                None
                if not holding_bars
                else str(
                    Decimal(sum(holding_bars)) / Decimal(len(holding_bars))
                )
            ),
            "exit_reason_counts": dict(sorted(reason_counts.items())),
        }
    )
    return metrics


def _delta(candidate: object, control: object) -> str | None:
    if candidate is None or control is None:
        return None
    return str(Decimal(str(candidate)) - Decimal(str(control)))


def _comparison(candidate: dict[str, object], control: dict[str, object]) -> dict:
    base = dict(quality._comparison(candidate, control))
    base.update(
        {
            "turnover_fraction_delta": _delta(
                candidate["turnover_fraction"], control["turnover_fraction"]
            ),
            "closed_trade_count_delta": _delta(
                candidate["closed_trade_count"], control["closed_trade_count"]
            ),
            "selection_exit_count_delta": _delta(
                candidate["selection_exit_count"], control["selection_exit_count"]
            ),
            "hard_stop_count_delta": _delta(
                candidate["hard_stop_count"], control["hard_stop_count"]
            ),
            "average_holding_bars_delta": _delta(
                candidate["average_holding_bars"], control["average_holding_bars"]
            ),
        }
    )
    return base


def qualify(
    csv_path: Path,
    trading_quality_config_path: Path,
    selection_exit_policy_path: Path,
) -> dict[str, object]:
    config = quality.load_config(trading_quality_config_path)
    policy = load_policy(selection_exit_policy_path)
    raw_bars = quality.read_csv(csv_path)
    bars = quality.synchronize_common_timestamps(
        raw_bars,
        minimum_symbols=quality._positive_int(config, "minimum_symbols"),
    )
    control = _run(bars, config=config, policy=policy, confirmed=False)
    candidate = _run(bars, config=config, policy=policy, confirmed=True)
    control_metrics = _extended_metrics(control)
    candidate_metrics = _extended_metrics(candidate)
    blockers = list(policy["promotion_blockers"])
    satisfied = [
        blocker
        for blocker in blockers
        if blocker == "NO_SELECTION_EXIT_SAME_SAMPLE_EVIDENCE"
    ]
    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_SELECTION_EXIT_CONFIRMATION_SHADOW_RESEARCH",
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
        "source_csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "symbols": sorted({bar.symbol for bar in bars}),
        "selection_exit_policy": policy["policy"],
        "scope": policy["scope"],
        "control_immediate_exit": control_metrics,
        "candidate_confirmed_exit": candidate_metrics,
        "comparison": _comparison(candidate_metrics, control_metrics),
        "evidence_satisfied_blockers": satisfied,
        "remaining_promotion_blockers": [
            blocker for blocker in blockers if blocker not in satisfied
        ],
        "limitations": list(policy.get("limitations", []))
        + [
            "SAME_SAMPLE_MARGINAL_EVIDENCE_IS_NOT_PROMOTION_EVIDENCE",
            "LOWER_TURNOVER_CAN_MASK_WORSE_TAIL_RISK",
            "HARD_STOP_COUNT_MUST_BE_REVIEWED_BEFORE_ANY_ACTIVATION",
            "NO_STRATEGY_PROFITABILITY_CLAIM",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Marginal shadow evaluation of selection-exit confirmation"
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
    policy = load_policy(args.selection_exit_policy)
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
                },
                sort_keys=True,
            )
        )
        return 0
    if args.csv is None or args.output is None:
        raise ValueError("csv and output are required for selection-exit qualification")
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
