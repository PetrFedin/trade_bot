from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioResult,
    PortfolioExitReason,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.position_management import TakeProfitMode
from tools import qualify_cross_sectional_trading_quality_shadow as quality

_SCHEMA = "profit-runner-shadow-evidence-v1"
_EXPECTED_POLICY_SCHEMA = "profit-runner-shadow-v1"


def load_policy(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != _EXPECTED_POLICY_SCHEMA:
        raise ValueError("unexpected profit-runner policy schema")
    if payload.get("shadow_only") is not True:
        raise ValueError("profit-runner policy must remain shadow_only")
    if payload.get("strategy_promotion_allowed") is not False:
        raise ValueError("profit-runner policy must not allow promotion")
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("profit-runner policy object is required")
    if policy.get("take_profit_mode") != TakeProfitMode.PROFIT_RUNNER.value:
        raise ValueError("profit-runner policy must declare PROFIT_RUNNER")
    if not isinstance(payload.get("promotion_blockers"), list):
        raise ValueError("profit-runner promotion blockers are required")
    return payload


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
    runner: bool,
    first_execution_index: int | None = None,
) -> CrossSectionalPortfolioResult:
    position_policy = quality._position_policy(config, protection=True)
    if runner:
        position_policy = replace(
            position_policy,
            take_profit_mode=TakeProfitMode.PROFIT_RUNNER,
        )
    return CrossSectionalPortfolioBacktester(
        selector=_selector(config),
        portfolio_policy=quality._portfolio_policy(config),
        position_policy=position_policy,
        reentry_policy=quality._reentry_policy(config),
        sizing_policy=quality._sizing_policy(config),
    ).run(bars, first_execution_index=first_execution_index)


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _extended_metrics(result: CrossSectionalPortfolioResult) -> dict[str, object]:
    metrics = dict(quality._result_metrics(result))
    reason_counts = Counter(trade.exit_reason.value for trade in result.closed_trades)
    holding = tuple(Decimal(trade.holding_bars) for trade in result.closed_trades)
    giveback = tuple(
        trade.mfe_giveback_fraction
        for trade in result.closed_trades
        if trade.mfe_giveback_fraction is not None
    )
    metrics.update(
        {
            "take_profit_exit_count": reason_counts[
                PortfolioExitReason.INTRABAR_TAKE_PROFIT.value
            ],
            "profit_protection_exit_count": reason_counts[
                PortfolioExitReason.INTRABAR_PROFIT_PROTECTION.value
            ],
            "trailing_stop_exit_count": reason_counts[
                PortfolioExitReason.INTRABAR_TRAILING_STOP.value
            ],
            "hard_stop_count": reason_counts[
                PortfolioExitReason.INTRABAR_HARD_STOP.value
            ],
            "average_holding_bars": (
                None if not holding else str(_mean(holding))
            ),
            "average_mfe_giveback_fraction": (
                None if not giveback else str(_mean(giveback))
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
    for field in (
        "turnover_fraction",
        "closed_trade_count",
        "take_profit_exit_count",
        "profit_protection_exit_count",
        "trailing_stop_exit_count",
        "hard_stop_count",
        "average_holding_bars",
        "average_mfe_giveback_fraction",
    ):
        base[f"{field}_delta"] = _delta(candidate[field], control[field])
    return base


def qualify(
    csv_path: Path,
    trading_quality_config_path: Path,
    profit_runner_policy_path: Path,
) -> dict[str, object]:
    config = quality.load_config(trading_quality_config_path)
    policy = load_policy(profit_runner_policy_path)
    raw_bars = quality.read_csv(csv_path)
    bars = quality.synchronize_common_timestamps(
        raw_bars,
        minimum_symbols=quality._positive_int(config, "minimum_symbols"),
    )
    control = _run(bars, config=config, runner=False)
    candidate = _run(bars, config=config, runner=True)
    control_metrics = _extended_metrics(control)
    candidate_metrics = _extended_metrics(candidate)
    blockers = list(policy["promotion_blockers"])
    satisfied = [
        blocker
        for blocker in blockers
        if blocker == "NO_PROFIT_RUNNER_SAME_SAMPLE_EVIDENCE"
    ]
    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_PROFIT_RUNNER_SHADOW_RESEARCH",
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
        "source_csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "symbols": sorted({bar.symbol for bar in bars}),
        "profit_runner_policy": policy["policy"],
        "scope": policy["scope"],
        "control_fixed_take_profit": control_metrics,
        "candidate_profit_runner": candidate_metrics,
        "comparison": _comparison(candidate_metrics, control_metrics),
        "evidence_satisfied_blockers": satisfied,
        "remaining_promotion_blockers": [
            blocker for blocker in blockers if blocker not in satisfied
        ],
        "limitations": list(policy.get("limitations", []))
        + [
            "SAME_SAMPLE_MARGINAL_EVIDENCE_IS_NOT_PROMOTION_EVIDENCE",
            "RUNNER_MUST_BE_JUDGED_ON_CAPTURE_GIVEBACK_AND_DRAWDOWN_TOGETHER",
            "NO_STRATEGY_PROFITABILITY_CLAIM",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Marginal shadow evaluation of fixed take-profit vs profit runner"
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
    policy = load_policy(args.profit_runner_policy)
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
        raise ValueError("csv and output are required for profit-runner qualification")
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
