from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoTradePlan

_BPS = Decimal("10000")


@dataclass(frozen=True)
class CryptoEntryEconomicsPolicy:
    minimum_expected_edge_to_target: Decimal = Decimal("1.25")
    maximum_round_trip_cost_to_target: Decimal = Decimal("0.15")
    minimum_target_to_risk_budget: Decimal = Decimal("1.50")

    def validate(self) -> None:
        if self.minimum_expected_edge_to_target < Decimal("1"):
            raise ValueError("expected-edge buffer cannot be below 1x target")
        if not Decimal("0") < self.maximum_round_trip_cost_to_target < Decimal("1"):
            raise ValueError("cost-to-target ceiling must be within (0, 1)")
        if self.minimum_target_to_risk_budget <= 0:
            raise ValueError("target-to-risk floor must be positive")


@dataclass(frozen=True)
class CryptoEntryEconomicsDecision:
    eligible: bool
    reasons: tuple[str, ...]
    expected_edge_to_target: Decimal
    round_trip_cost_to_target: Decimal
    target_to_risk_budget: Decimal
    shadow_only: bool = True
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False


@dataclass(frozen=True)
class CryptoActualFeeEntryDecision:
    """Fail-closed recheck after account fee tier and order quantity are known."""

    eligible: bool
    reasons: tuple[str, ...]
    execution_notional_usdt: Decimal
    taker_fee_rate: Decimal
    modeled_round_trip_cost_usdt: Decimal
    modeled_stop_loss_after_cost_usdt: Decimal
    modeled_expected_net_edge_usd: Decimal
    required_move_fraction: Decimal
    demo_activation_allowed: bool
    live_activation_allowed: bool = False


def evaluate_entry_economics(
    plan: CryptoTradePlan,
    policy: CryptoEntryEconomicsPolicy | None = None,
) -> CryptoEntryEconomicsDecision:
    """Evaluate whether a planned trade has enough economic margin above execution costs.

    This is a research candidate, not an active execution gate. Its purpose is to reduce
    turnover where the requested dollar target is too small relative to modeled costs or
    where the expected move only barely clears the target.
    """

    active_policy = CryptoEntryEconomicsPolicy() if policy is None else policy
    active_policy.validate()
    if plan.target_net_profit_usd <= 0 or plan.risk_budget_usdt <= 0:
        raise ValueError("crypto trade plan target and risk budget must be positive")
    if plan.estimated_round_trip_cost_usdt < 0:
        raise ValueError("crypto trade plan execution cost cannot be negative")

    edge_multiple = plan.expected_net_edge_usd / plan.target_net_profit_usd
    cost_ratio = plan.estimated_round_trip_cost_usdt / plan.target_net_profit_usd
    target_risk = plan.target_net_profit_usd / plan.risk_budget_usdt
    reasons: list[str] = []
    if edge_multiple < active_policy.minimum_expected_edge_to_target:
        reasons.append("EXPECTED_EDGE_BUFFER_TOO_THIN")
    if cost_ratio > active_policy.maximum_round_trip_cost_to_target:
        reasons.append("EXECUTION_COST_SHARE_TOO_HIGH")
    if target_risk < active_policy.minimum_target_to_risk_budget:
        reasons.append("TARGET_TO_RISK_RATIO_TOO_LOW")
    return CryptoEntryEconomicsDecision(
        eligible=not reasons,
        reasons=tuple(reasons),
        expected_edge_to_target=edge_multiple,
        round_trip_cost_to_target=cost_ratio,
        target_to_risk_budget=target_risk,
    )


def revalidate_entry_at_actual_taker_fee(
    plan: CryptoTradePlan,
    *,
    execution_notional_usdt: Decimal,
    actual_taker_fee_rate: Decimal,
    strategy_config: CryptoPerpStrategyConfig,
) -> CryptoActualFeeEntryDecision:
    """Reprice the planned edge/risk using the account fee tier before a demo order is sent.

    ``execution_notional_usdt`` should use the instrument-normalized order quantity rather than
    the pre-quantization plan notional. This prevents quantity rounding or a higher account fee
    tier from silently invalidating the minimum net-profit and risk-budget admission checks.
    """

    strategy_config.validate()
    if execution_notional_usdt <= 0 or not execution_notional_usdt.is_finite():
        raise ValueError("actual-fee entry notional must be positive and finite")
    if (
        not actual_taker_fee_rate.is_finite()
        or actual_taker_fee_rate < 0
        or actual_taker_fee_rate >= Decimal("1")
    ):
        raise ValueError("actual taker fee rate must be finite and within [0, 1)")
    if plan.target_net_profit_usd <= 0 or plan.risk_budget_usdt <= 0:
        raise ValueError("crypto trade plan target and risk budget must be positive")

    per_fill_cost_fraction = (
        actual_taker_fee_rate + strategy_config.slippage_bps_per_fill / _BPS
    )
    round_trip_cost_fraction = per_fill_cost_fraction * Decimal("2")
    round_trip_cost = execution_notional_usdt * round_trip_cost_fraction
    stop_loss_after_cost = execution_notional_usdt * (
        plan.stop_fraction + round_trip_cost_fraction
    )
    expected_net_edge = (
        execution_notional_usdt * plan.expected_move_fraction - round_trip_cost
    )
    required_move = (
        plan.target_net_profit_usd / execution_notional_usdt + round_trip_cost_fraction
    )

    reasons: list[str] = []
    if plan.expected_move_fraction < required_move:
        reasons.append("ACCOUNT_FEE_TARGET_NET_EDGE_UNAVAILABLE")
    if expected_net_edge < plan.target_net_profit_usd:
        reasons.append("ACCOUNT_FEE_EXPECTED_NET_PROFIT_BELOW_TARGET")
    if stop_loss_after_cost > plan.risk_budget_usdt:
        reasons.append("ACCOUNT_FEE_RISK_BUDGET_EXCEEDED")

    return CryptoActualFeeEntryDecision(
        eligible=not reasons,
        reasons=tuple(reasons),
        execution_notional_usdt=execution_notional_usdt,
        taker_fee_rate=actual_taker_fee_rate,
        modeled_round_trip_cost_usdt=round_trip_cost,
        modeled_stop_loss_after_cost_usdt=stop_loss_after_cost,
        modeled_expected_net_edge_usd=expected_net_edge,
        required_move_fraction=required_move,
        demo_activation_allowed=not reasons,
    )