from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.marketdata.bybit_v5 import BybitKlineAcquisition
from app.strategy.crypto_correlation import CryptoCorrelationPolicy
from app.strategy.crypto_execution_risk import CryptoExecutionRiskPolicy
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_runner_admission import CryptoRunnerAdmissionPolicy
from app.strategy.crypto_session_risk import CryptoSessionRiskPolicy
from app.strategy.crypto_trade_management import CryptoProtectionPolicy
from tools.replay_bybit_crypto_runner import replay_open_ended_crypto_runner

_CONDITIONAL_EDGE_MULTIPLE = Decimal("1.50")
_TIGHT_PROFIT_LOCK_ACTIVATION_R = Decimal("1.00")
_TIGHT_PROFIT_LOCK_R = Decimal("0.50")


def run_crypto_strategy_v2_suite(
    acquisition: BybitKlineAcquisition,
    *,
    opening_equity_usdt: Decimal = Decimal("1000"),
    base_config: CryptoPerpStrategyConfig | None = None,
    protection_policy: CryptoProtectionPolicy | None = None,
    interval: str = "5",
) -> dict[str, Any]:
    """Run predeclared crypto-v2 shadow candidates on one identical acquisition."""

    if opening_equity_usdt <= 0:
        raise ValueError("crypto strategy-v2 opening equity must be positive")
    admission = CryptoRunnerAdmissionPolicy(
        minimum_expected_edge_multiple=_CONDITIONAL_EDGE_MULTIPLE
    )
    session = CryptoSessionRiskPolicy()
    correlation = CryptoCorrelationPolicy()
    execution = CryptoExecutionRiskPolicy()
    baseline_protection = (
        CryptoProtectionPolicy() if protection_policy is None else protection_policy
    )
    baseline_protection.validate()
    tight_profit_lock = CryptoProtectionPolicy(
        break_even_activation_r=baseline_protection.break_even_activation_r,
        profit_lock_activation_r=_TIGHT_PROFIT_LOCK_ACTIVATION_R,
        profit_lock_r=_TIGHT_PROFIT_LOCK_R,
        maximum_holding_bars=baseline_protection.maximum_holding_bars,
        cooldown_bars_after_stop=baseline_protection.cooldown_bars_after_stop,
        cooldown_bars_after_target=baseline_protection.cooldown_bars_after_target,
    )
    tight_profit_lock.validate()

    common = {
        "opening_equity_usdt": opening_equity_usdt,
        "base_config": base_config,
        "protection_policy": baseline_protection,
        "runner_admission_policy": admission,
        "interval": interval,
    }
    candidates = {
        "CONDITIONAL_1_5X": replay_open_ended_crypto_runner(
            acquisition,
            **common,
        ),
        "CONDITIONAL_TIGHT_PROFIT_LOCK": replay_open_ended_crypto_runner(
            acquisition,
            protection_policy=tight_profit_lock,
            opening_equity_usdt=opening_equity_usdt,
            base_config=base_config,
            runner_admission_policy=admission,
            interval=interval,
        ),
        "CONDITIONAL_SESSION_RISK": replay_open_ended_crypto_runner(
            acquisition,
            session_risk_policy=session,
            **common,
        ),
        "CONDITIONAL_DIVERSIFIED": replay_open_ended_crypto_runner(
            acquisition,
            correlation_policy=correlation,
            **common,
        ),
        "CONDITIONAL_EXECUTION_RISK": replay_open_ended_crypto_runner(
            acquisition,
            execution_risk_policy=execution,
            **common,
        ),
        "CONDITIONAL_COMBINED_RISK": replay_open_ended_crypto_runner(
            acquisition,
            session_risk_policy=session,
            correlation_policy=correlation,
            execution_risk_policy=execution,
            **common,
        ),
    }
    for name, candidate in candidates.items():
        if candidate["strategy_promotion_allowed"] is not False:
            raise ValueError(f"crypto strategy-v2 candidate {name} became promotable")
        if candidate["bybit_live_order_routing_allowed"] is not False:
            raise ValueError(f"crypto strategy-v2 candidate {name} enabled live routing")

    return {
        "suite": "BYBIT_CRYPTO_STRATEGY_V2_SHADOW",
        "candidate_contract": {
            "minimum_entry_net_profit_usd": 20.0,
            "runner_minimum_expected_edge_multiple": 1.5,
            "tight_profit_lock_hypothesis": {
                "break_even_activation_r": float(
                    tight_profit_lock.break_even_activation_r
                ),
                "profit_lock_activation_r": float(
                    tight_profit_lock.profit_lock_activation_r
                ),
                "profit_lock_r": float(tight_profit_lock.profit_lock_r),
                "same_sample_14d_promotion_allowed": False,
                "requires_walk_forward_validation": True,
            },
            "session_risk_policy": {
                "maximum_realized_loss_fraction": float(
                    session.maximum_realized_loss_fraction
                ),
                "maximum_drawdown_fraction": float(session.maximum_drawdown_fraction),
                "maximum_execution_cost_fraction": float(
                    session.maximum_execution_cost_fraction
                ),
                "maximum_consecutive_losses": session.maximum_consecutive_losses,
                "minimum_equity_fraction": float(session.minimum_equity_fraction),
            },
            "correlation_policy": {
                "lookback_bars": correlation.lookback_bars,
                "minimum_return_observations": correlation.minimum_return_observations,
                "maximum_pairwise_correlation": float(
                    correlation.maximum_pairwise_correlation
                ),
            },
            "execution_risk_policy": {
                "maximum_risk_budget_multiple": float(
                    execution.maximum_risk_budget_multiple
                ),
            },
        },
        "candidates": candidates,
        "strategy_promotion_allowed": False,
        "demo_order_writes_enabled": False,
        "live_promotion_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def compact_candidate_comparison(suite: dict[str, Any]) -> dict[str, Any]:
    candidates = suite.get("candidates")
    if not isinstance(candidates, dict):
        raise ValueError("crypto strategy-v2 suite candidates are missing")
    comparison: dict[str, Any] = {}
    for name, candidate in candidates.items():
        if not isinstance(candidate, dict):
            raise ValueError("crypto strategy-v2 candidate must be an object")
        metrics = candidate.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("crypto strategy-v2 candidate metrics are missing")
        comparison[name] = {
            "closed_trade_count": metrics["closed_trade_count"],
            "winning_trade_count": metrics["win_count"],
            "losing_trade_count": metrics["loss_count"],
            "total_net_pnl_usdt": metrics["total_net_pnl_usdt"],
            "profit_factor": metrics["profit_factor"],
            "maximum_drawdown_pct": metrics["maximum_drawdown_pct"],
            "fees_usdt": metrics["fees_usdt"],
            "risk_budget_breach_count": metrics["risk_budget_breach_count"],
            "accepted_trade_plan_event_count": candidate[
                "accepted_trade_plan_event_count"
            ],
            "runner_selected_trade_count": candidate["runner_selected_trade_count"],
            "fixed_target_selected_trade_count": candidate[
                "fixed_target_selected_trade_count"
            ],
            "session_risk": candidate["session_risk"],
            "correlation_diversification": candidate[
                "correlation_diversification"
            ],
            "execution_risk": candidate["execution_risk"],
        }
    return comparison
