from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.strategy.crypto_perp import CryptoTradePlan


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
