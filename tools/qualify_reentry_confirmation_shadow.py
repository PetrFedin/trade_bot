from __future__ import annotations

import argparse
import json
from collections import Counter
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from app.marketdata.historical import HistoricalDataPolicy
from app.marketdata.manifest import ManifestedCsvHistoricalBarSource
from app.strategy.backtest import BacktestConfig
from app.strategy.managed_backtest import (
    ClosedTrade,
    DecisionAction,
    ManagedBacktestResult,
    ManagedHistoricalBacktester,
    StrategyDecisionTrace,
)
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from app.strategy.regime_momentum import (
    RegimeAwareMomentumConfig,
    RegimeAwareMomentumStrategy,
)

_POLICY_SCHEMA = "reentry-confirmation-shadow-v1"
_EVIDENCE_SCHEMA = "reentry-confirmation-evidence-v1"


def _decimal(data: dict[str, Any], field: str) -> Decimal:
    value = data.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal string")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _positive_int(data: dict[str, Any], field: str) -> int:
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _object(data: dict[str, Any], field: str) -> dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def load_policy(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _POLICY_SCHEMA:
        raise ValueError("re-entry confirmation policy schema mismatch")
    if data.get("shadow_only") is not True or data.get("promotion_allowed") is not False:
        raise ValueError("re-entry confirmation candidate must remain shadow-only")
    reentry = _object(data, "reentry_confirmation")
    if reentry.get("initial_entry_requires_confirmation") is not False:
        raise ValueError("initial entry must remain unchanged")
    if reentry.get("reset_streak_on_ineligible_signal") is not True:
        raise ValueError("re-entry streak must reset on ineligible signal")
    if reentry.get("apply_after_any_exit") is not True:
        raise ValueError("re-entry confirmation must apply after any exit")
    _positive_int(reentry, "minimum_consecutive_eligible_bars")
    return data


def _build(
    policy: dict[str, Any], *, strategy_id: str
) -> tuple[
    RegimeAwareMomentumStrategy,
    PositionManagementPolicy,
    ReentryConfirmationPolicy,
    BacktestConfig,
    int,
]:
    signal = _object(policy, "signal")
    position = _object(policy, "position_management")
    reentry = _object(policy, "reentry_confirmation")
    strategy = RegimeAwareMomentumStrategy(
        strategy_id=strategy_id,
        target_quantity=_decimal(policy, "target_quantity"),
        config=RegimeAwareMomentumConfig(
            fast_bars=_positive_int(signal, "fast_bars"),
            slow_bars=_positive_int(signal, "slow_bars"),
            momentum_lookback_bars=_positive_int(signal, "momentum_lookback_bars"),
            volatility_bars=_positive_int(signal, "volatility_bars"),
            minimum_momentum_return=_decimal(signal, "minimum_momentum_return"),
            minimum_trend_strength=_decimal(signal, "minimum_trend_strength"),
            maximum_realized_volatility=_decimal(signal, "maximum_realized_volatility"),
        ),
    )
    position_policy = PositionManagementPolicy(
        stop_loss_fraction=_decimal(position, "stop_loss_fraction"),
        take_profit_fraction=_decimal(position, "take_profit_fraction"),
        trailing_activation_fraction=_decimal(position, "trailing_activation_fraction"),
        trailing_stop_fraction=_decimal(position, "trailing_stop_fraction"),
        maximum_holding_bars=_positive_int(position, "maximum_holding_bars"),
    )
    reentry_policy = ReentryConfirmationPolicy(
        minimum_consecutive_eligible_bars=_positive_int(
            reentry, "minimum_consecutive_eligible_bars"
        ),
        initial_entry_requires_confirmation=False,
        reset_streak_on_ineligible_signal=True,
        apply_after_any_exit=True,
    )
    backtest = BacktestConfig(
        opening_cash=_decimal(policy, "opening_cash"),
        fee_per_fill=_decimal(policy, "fee_per_fill"),
        slippage_bps=_decimal(policy, "slippage_bps"),
    )
    return (
        strategy,
        position_policy,
        reentry_policy,
        backtest,
        _positive_int(policy, "training_bars"),
    )


def _one_bar_reentries(trace: tuple[StrategyDecisionTrace, ...]) -> int:
    return sum(
        previous.action is DecisionAction.EXIT
        and current.action is DecisionAction.ENTER
        and current.execution_index == previous.execution_index + 1
        for previous, current in pairwise(trace)
    )


def _pending_reentry_blocks(trace: tuple[StrategyDecisionTrace, ...]) -> int:
    return sum(item.entry_block_reason is not None for item in trace)


def _trade_totals(trades: list[ClosedTrade]) -> dict[str, object]:
    gross_profit = sum(
        (trade.net_pnl for trade in trades if trade.net_pnl > 0), Decimal("0")
    )
    gross_loss = sum(
        (trade.net_pnl for trade in trades if trade.net_pnl < 0), Decimal("0")
    )
    profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else None
    return {
        "closed_trade_count": len(trades),
        "winning_trades": sum(trade.net_pnl > 0 for trade in trades),
        "losing_trades": sum(trade.net_pnl < 0 for trade in trades),
        "gross_profit": str(gross_profit),
        "gross_loss": str(gross_loss),
        "net_closed_trade_pnl": str(gross_profit + gross_loss),
        "profit_factor": None if profit_factor is None else str(profit_factor),
    }


def _result(result: ManagedBacktestResult) -> dict[str, object]:
    actions = Counter(item.action.value for item in result.decision_trace)
    return {
        "total_pnl": str(result.total_pnl),
        "total_return": str(result.total_return),
        "max_drawdown": str(result.max_drawdown),
        "average_maximum_adverse_excursion_fraction": str(
            result.average_maximum_adverse_excursion_fraction
        ),
        "one_bar_reentry_count": _one_bar_reentries(result.decision_trace),
        "pending_reentry_blocks": _pending_reentry_blocks(result.decision_trace),
        "decision_action_counts": dict(sorted(actions.items())),
        **_trade_totals(list(result.closed_trades)),
    }


def qualify(manifest_path: Path, policy_path: Path) -> dict[str, object]:
    policy = load_policy(policy_path)
    baseline_id = str(policy["baseline_strategy_id"])
    candidate_id = str(policy["candidate_strategy_id"])
    (
        baseline_strategy,
        position_policy,
        reentry_policy,
        backtest,
        training_bars,
    ) = _build(policy, strategy_id=baseline_id)
    candidate_strategy, _, _, _, _ = _build(policy, strategy_id=candidate_id)
    manifested = ManifestedCsvHistoricalBarSource(
        manifest_path,
        policy=HistoricalDataPolicy(
            minimum_bars=60,
            maximum_jump_fraction=Decimal("0.25"),
        ),
    ).load()

    regimes: list[dict[str, object]] = []
    baseline_trades: list[ClosedTrade] = []
    candidate_trades: list[ClosedTrade] = []
    baseline_returns: list[Decimal] = []
    candidate_returns: list[Decimal] = []
    baseline_drawdowns: list[Decimal] = []
    candidate_drawdowns: list[Decimal] = []
    baseline_one_bar_reentries = 0
    candidate_one_bar_reentries = 0
    candidate_pending_blocks = 0

    for window in manifested.manifest.windows:
        bars = [
            bar
            for bar in manifested.dataset.bars
            if window.start <= bar.timestamp < window.end
        ]
        baseline = ManagedHistoricalBacktester(
            strategy=baseline_strategy,
            position_policy=position_policy,
            config=backtest,
        ).run(bars, first_execution_index=training_bars)
        candidate = ManagedHistoricalBacktester(
            strategy=candidate_strategy,
            position_policy=position_policy,
            reentry_policy=reentry_policy,
            config=backtest,
        ).run(bars, first_execution_index=training_bars)
        baseline_trades.extend(baseline.closed_trades)
        candidate_trades.extend(candidate.closed_trades)
        baseline_returns.append(baseline.total_return)
        candidate_returns.append(candidate.total_return)
        baseline_drawdown = baseline.max_drawdown / backtest.opening_cash
        candidate_drawdown = candidate.max_drawdown / backtest.opening_cash
        baseline_drawdowns.append(baseline_drawdown)
        candidate_drawdowns.append(candidate_drawdown)
        baseline_reentries = _one_bar_reentries(baseline.decision_trace)
        candidate_reentries = _one_bar_reentries(candidate.decision_trace)
        pending_blocks = _pending_reentry_blocks(candidate.decision_trace)
        baseline_one_bar_reentries += baseline_reentries
        candidate_one_bar_reentries += candidate_reentries
        candidate_pending_blocks += pending_blocks
        regimes.append(
            {
                "name": window.name,
                "baseline": _result(baseline),
                "candidate": _result(candidate),
                "comparison": {
                    "return_delta": str(candidate.total_return - baseline.total_return),
                    "drawdown_fraction_delta": str(
                        candidate_drawdown - baseline_drawdown
                    ),
                    "average_mae_delta": str(
                        candidate.average_maximum_adverse_excursion_fraction
                        - baseline.average_maximum_adverse_excursion_fraction
                    ),
                    "one_bar_reentry_delta": candidate_reentries - baseline_reentries,
                },
            }
        )

    regime_count = Decimal(len(regimes))
    mean_baseline_return = sum(baseline_returns, Decimal("0")) / regime_count
    mean_candidate_return = sum(candidate_returns, Decimal("0")) / regime_count
    baseline_trade_totals = _trade_totals(baseline_trades)
    candidate_trade_totals = _trade_totals(candidate_trades)
    candidate_profit_factor = candidate_trade_totals["profit_factor"]
    candidate_net_pnl = Decimal(str(candidate_trade_totals["net_closed_trade_pnl"]))
    blockers = [
        "SHADOW_ONLY_POLICY",
        "THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE",
        "SINGLE_SYMBOL_HISTORICAL_EVIDENCE",
        "NO_EXTERNAL_PAPER_STRATEGY_EVIDENCE",
        "REENTRY_CONFIRMATION_SAMPLE_TOO_SMALL",
    ]
    if candidate_profit_factor is None or Decimal(str(candidate_profit_factor)) < 1:
        blockers.append("CANDIDATE_AGGREGATE_PROFIT_FACTOR_BELOW_ONE")
    if candidate_net_pnl <= 0:
        blockers.append("CANDIDATE_CLOSED_TRADE_PNL_NOT_POSITIVE")

    return {
        "schema_version": _EVIDENCE_SCHEMA,
        "shadow_only": True,
        "promotion_allowed": False,
        "promotion_blockers": blockers,
        "dataset_id": manifested.dataset.dataset_id,
        "dataset_sha256": manifested.dataset.canonical_sha256,
        "source_classification": manifested.manifest.source_classification,
        "baseline_strategy_id": baseline_id,
        "candidate_strategy_id": candidate_id,
        "predeclared_policy": policy,
        "aggregate": {
            "baseline": baseline_trade_totals,
            "candidate": candidate_trade_totals,
            "mean_baseline_return": str(mean_baseline_return),
            "mean_candidate_return": str(mean_candidate_return),
            "mean_return_delta": str(mean_candidate_return - mean_baseline_return),
            "worst_baseline_drawdown_fraction": str(max(baseline_drawdowns)),
            "worst_candidate_drawdown_fraction": str(max(candidate_drawdowns)),
            "baseline_one_bar_reentry_count": baseline_one_bar_reentries,
            "candidate_one_bar_reentry_count": candidate_one_bar_reentries,
            "candidate_pending_reentry_blocks": candidate_pending_blocks,
            "one_bar_reentry_reduced": (
                candidate_one_bar_reentries < baseline_one_bar_reentries
            ),
            "candidate_closed_trade_pnl_not_worse": (
                candidate_net_pnl
                >= Decimal(str(baseline_trade_totals["net_closed_trade_pnl"]))
            ),
            "candidate_mean_return_not_worse": (
                mean_candidate_return >= mean_baseline_return
            ),
            "candidate_worst_drawdown_not_worse": (
                max(candidate_drawdowns) <= max(baseline_drawdowns)
            ),
        },
        "regimes": regimes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate re-entry confirmation shadow evidence"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    evidence = qualify(args.manifest, args.policy)
    payload = json.dumps(evidence, indent=2, sort_keys=True)
    print(payload)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
