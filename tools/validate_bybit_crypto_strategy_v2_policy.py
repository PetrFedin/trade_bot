from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_lifecycle_gate import BybitDemoLifecyclePolicy
from app.strategy.crypto_correlation import CryptoCorrelationPolicy
from app.strategy.crypto_execution_risk import CryptoExecutionRiskPolicy
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_profit_runner import CryptoProfitRunnerPolicy
from app.strategy.crypto_runner_admission import CryptoRunnerAdmissionPolicy
from app.strategy.crypto_session_risk import CryptoSessionRiskPolicy
from tools.qualify_bybit_crypto_walk_forward import CryptoWalkForwardPolicy

_SCHEMA = "bybit-crypto-strategy-v2-policy-1"


def validate_strategy_v2_policy(policy: dict[str, Any]) -> dict[str, Any]:
    if policy.get("schema_version") != _SCHEMA:
        raise ValueError("Bybit crypto strategy-v2 policy schema mismatch")
    if policy.get("instrument_scope") != "BYBIT_USDT_PERPETUALS":
        raise ValueError("Bybit crypto strategy-v2 instrument scope changed")
    if policy.get("bar_interval") != "5m":
        raise ValueError("Bybit crypto strategy-v2 bar interval changed")

    strategy = CryptoPerpStrategyConfig().with_target(Decimal("20"))
    runner = CryptoProfitRunnerPolicy()
    admission = CryptoRunnerAdmissionPolicy()
    correlation = CryptoCorrelationPolicy()
    execution = CryptoExecutionRiskPolicy()
    session = CryptoSessionRiskPolicy()
    walk_forward = CryptoWalkForwardPolicy()
    lifecycle = BybitDemoLifecyclePolicy()

    entry = _object(policy, "entry")
    _equal_decimal(
        entry,
        "minimum_expected_net_profit_usd",
        strategy.target_net_profit_usd,
    )
    if entry.get("allow_15_usd_entry_fallback") is not False:
        raise ValueError("$15 entry fallback must remain disabled")
    _equal_decimal(
        entry,
        "cost_aware_risk_fraction_per_trade",
        strategy.risk_fraction_per_trade,
    )
    _equal_decimal(
        entry,
        "maximum_notional_to_equity",
        strategy.maximum_notional_to_equity,
    )
    execution_policy = _object(entry, "next_open_execution_risk")
    _equal_decimal(
        execution_policy,
        "maximum_risk_budget_multiple",
        execution.maximum_risk_budget_multiple,
    )
    if execution_policy.get("quantity_may_only_decrease") is not True:
        raise ValueError("next-open execution quantity must remain downsize-only")
    if execution_policy.get("cancel_if_minimum_net_edge_lost") is not True:
        raise ValueError("next-open minimum net-edge cancellation must remain enabled")

    runner_policy = _object(policy, "runner")
    _equal_decimal(
        runner_policy,
        "activation_net_profit_usd",
        runner.activation_net_profit_usd,
    )
    _equal_decimal(
        runner_policy,
        "initial_protected_net_profit_objective_usd",
        runner.protected_net_profit_usd,
    )
    _equal_decimal(
        runner_policy,
        "minimum_expected_edge_multiple",
        admission.minimum_expected_edge_multiple,
    )
    required_runner_net = (
        runner.activation_net_profit_usd * admission.minimum_expected_edge_multiple
    )
    _equal_decimal(
        runner_policy,
        "minimum_expected_net_profit_usd_for_runner",
        required_runner_net,
    )
    if runner_policy.get("fixed_20_target_when_runner_gate_fails") is not True:
        raise ValueError("conditional runner fixed-target fallback changed")
    if runner_policy.get("unconditional_runner_is_benchmark_only") is not True:
        raise ValueError("unconditional runner must remain benchmark-only")

    portfolio = _object(policy, "portfolio")
    if portfolio.get("maximum_concurrent_positions") != strategy.maximum_concurrent_positions:
        raise ValueError("crypto maximum concurrent positions drifted")
    correlation_policy = _object(portfolio, "correlation_shadow_candidate")
    if correlation_policy.get("lookback_bars") != correlation.lookback_bars:
        raise ValueError("correlation lookback drifted")
    if (
        correlation_policy.get("minimum_return_observations")
        != correlation.minimum_return_observations
    ):
        raise ValueError("correlation minimum observations drifted")
    _equal_decimal(
        correlation_policy,
        "maximum_positive_pairwise_correlation",
        correlation.maximum_pairwise_correlation,
    )
    if correlation_policy.get("negative_correlation_penalized") is not False:
        raise ValueError("negative correlation must not be penalized")
    if correlation_policy.get("insufficient_peer_history_fails_closed") is not True:
        raise ValueError("correlation history must fail closed")

    session_policy = _object(policy, "session_risk_shadow_candidate")
    _equal_decimal(
        session_policy,
        "maximum_realized_loss_fraction",
        session.maximum_realized_loss_fraction,
    )
    _equal_decimal(
        session_policy,
        "maximum_drawdown_fraction",
        session.maximum_drawdown_fraction,
    )
    _equal_decimal(
        session_policy,
        "maximum_execution_cost_fraction",
        session.maximum_execution_cost_fraction,
    )
    if session_policy.get("maximum_consecutive_losses") != session.maximum_consecutive_losses:
        raise ValueError("session consecutive-loss limit drifted")
    _equal_decimal(
        session_policy,
        "minimum_equity_fraction",
        session.minimum_equity_fraction,
    )
    if session_policy.get("forced_flatten_executes_at_next_open") is not True:
        raise ValueError("session-risk flatten timing changed")

    walk = _object(policy, "walk_forward")
    if walk.get("fold_days") != walk_forward.fold_days:
        raise ValueError("walk-forward fold length drifted")
    if walk.get("minimum_folds") != walk_forward.minimum_folds:
        raise ValueError("walk-forward minimum fold count drifted")
    if walk.get("minimum_total_closed_trades") != walk_forward.minimum_total_closed_trades:
        raise ValueError("walk-forward minimum closed trades drifted")
    _equal_decimal(
        walk,
        "minimum_positive_fold_fraction",
        walk_forward.minimum_positive_fold_fraction,
    )
    _equal_decimal(
        walk,
        "minimum_aggregate_profit_factor",
        walk_forward.minimum_aggregate_profit_factor,
    )
    _equal_decimal(
        walk,
        "maximum_worst_fold_drawdown_pct",
        walk_forward.maximum_worst_fold_drawdown_pct,
    )
    if walk.get("require_zero_risk_budget_breaches") is not True:
        raise ValueError("walk-forward risk breaches must remain disallowed")
    for field in (
        "cross_fold_signal_history_carried",
        "cross_fold_position_state_carried",
        "parameter_tuning_between_folds",
    ):
        if walk.get(field) is not False:
            raise ValueError(f"walk-forward leakage boundary changed: {field}")

    post_trade = _object(policy, "post_trade_accounting")
    if (
        post_trade.get("account_closed_pnl_reconciliation_required_before_symbol_reuse")
        is not lifecycle.require_account_closed_pnl_before_next_entry
    ):
        raise ValueError("post-trade closed-PnL reuse gate drifted")
    if (
        post_trade.get("funding_reconciliation_required_before_symbol_reuse")
        is not lifecycle.require_funding_before_next_entry
    ):
        raise ValueError("post-trade funding reuse gate drifted")
    if post_trade.get("funding_is_never_assumed_zero_when_unreconciled") is not True:
        raise ValueError("unreconciled funding must never be assumed zero")
    if post_trade.get("fully_reconciled_net_pnl_requires_funding_coverage") is not True:
        raise ValueError("full PnL must require funding coverage")
    if post_trade.get("profit_outcome_requires_fully_reconciled_all_in_pnl") is not True:
        raise ValueError("profit outcome must require fully reconciled all-in PnL")

    promotion = _object(policy, "promotion")
    for field in (
        "strategy_promotion_allowed",
        "demo_observation_automatic_activation_allowed",
        "live_promotion_allowed",
        "bybit_live_order_routing_allowed",
    ):
        if promotion.get(field) is not False:
            raise ValueError(f"strategy-v2 safety flag must remain false: {field}")

    return {
        "qualification": "PASS_BYBIT_CRYPTO_STRATEGY_V2_POLICY",
        "schema_version": _SCHEMA,
        "policy_matches_code_defaults": True,
        "strategy_promotion_allowed": False,
        "live_promotion_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def load_and_validate_strategy_v2_policy(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Bybit crypto strategy-v2 policy must be an object")
    return validate_strategy_v2_policy(raw)


def _object(data: dict[str, Any], field: str) -> dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"strategy-v2 policy {field} must be an object")
    return value


def _equal_decimal(data: dict[str, Any], field: str, expected: Decimal) -> None:
    value = data.get(field)
    parsed = Decimal(str(value))
    if parsed != expected:
        raise ValueError(
            f"strategy-v2 policy {field} drifted: {parsed} != {expected}"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Bybit crypto strategy-v2 policy")
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("research/bybit_crypto_strategy_v2_policy.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = load_and_validate_strategy_v2_policy(args.policy)
    print("BYBIT_CRYPTO_STRATEGY_V2_POLICY=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
