from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class CryptoResearchPosture(StrEnum):
    HOLD_SHADOW_INSUFFICIENT_SAMPLE = "HOLD_SHADOW_INSUFFICIENT_SAMPLE"
    RETUNE_TARGET_FEASIBILITY = "RETUNE_TARGET_FEASIBILITY"
    RETUNE_NEGATIVE_ECONOMICS = "RETUNE_NEGATIVE_ECONOMICS"
    RETUNE_RISK_QUALITY = "RETUNE_RISK_QUALITY"
    ELIGIBLE_FOR_DEMO_OBSERVATION = "ELIGIBLE_FOR_DEMO_OBSERVATION"


@dataclass(frozen=True)
class CryptoReplayEvidence:
    target_net_profit_usd: Decimal
    opening_equity_usdt: Decimal
    closed_trade_count: int
    accepted_trade_plan_event_count: int
    total_net_pnl_usdt: Decimal
    profit_factor: Decimal | None
    maximum_drawdown_pct: Decimal
    fees_usdt: Decimal
    risk_budget_breach_count: int
    observed_days: Decimal

    def validate(self) -> None:
        if self.target_net_profit_usd <= 0:
            raise ValueError("crypto evidence target must be positive")
        if self.opening_equity_usdt <= 0:
            raise ValueError("crypto evidence opening equity must be positive")
        if self.closed_trade_count < 0 or self.accepted_trade_plan_event_count < 0:
            raise ValueError("crypto evidence counts cannot be negative")
        if self.maximum_drawdown_pct < 0:
            raise ValueError("crypto evidence drawdown cannot be negative")
        if self.fees_usdt < 0:
            raise ValueError("crypto evidence fees cannot be negative")
        if self.risk_budget_breach_count < 0:
            raise ValueError("crypto evidence risk breaches cannot be negative")
        if self.observed_days <= 0:
            raise ValueError("crypto evidence observed days must be positive")
        if self.profit_factor is not None and self.profit_factor < 0:
            raise ValueError("crypto evidence profit factor cannot be negative")


@dataclass(frozen=True)
class CryptoEvidencePolicy:
    minimum_closed_trades_for_demo_observation: int = 30
    minimum_observed_days_for_demo_observation: Decimal = Decimal("14")
    minimum_profit_factor: Decimal = Decimal("1.20")
    maximum_drawdown_pct: Decimal = Decimal("5")
    maximum_fees_to_opening_equity: Decimal = Decimal("0.10")
    require_positive_net_pnl: bool = True
    require_zero_risk_budget_breaches: bool = True

    def validate(self) -> None:
        if self.minimum_closed_trades_for_demo_observation <= 0:
            raise ValueError("minimum crypto evidence sample must be positive")
        if self.minimum_observed_days_for_demo_observation <= 0:
            raise ValueError("minimum crypto evidence days must be positive")
        if self.minimum_profit_factor <= 0:
            raise ValueError("minimum crypto evidence profit factor must be positive")
        if self.maximum_drawdown_pct <= 0:
            raise ValueError("maximum crypto evidence drawdown must be positive")
        if not Decimal("0") < self.maximum_fees_to_opening_equity < Decimal("1"):
            raise ValueError("crypto evidence fee ratio must be within (0, 1)")


@dataclass(frozen=True)
class CryptoEvidenceDecision:
    posture: CryptoResearchPosture
    reasons: tuple[str, ...]
    demo_observation_allowed: bool
    live_promotion_allowed: bool = False


def evaluate_crypto_replay_evidence(
    evidence: CryptoReplayEvidence,
    policy: CryptoEvidencePolicy | None = None,
) -> CryptoEvidenceDecision:
    """Turn historical replay metrics into a research posture, never a live promotion."""

    evidence.validate()
    active_policy = CryptoEvidencePolicy() if policy is None else policy
    active_policy.validate()

    if evidence.accepted_trade_plan_event_count == 0:
        return CryptoEvidenceDecision(
            posture=CryptoResearchPosture.RETUNE_TARGET_FEASIBILITY,
            reasons=("NO_TRADE_PLAN_MET_TARGET_NET_EDGE",),
            demo_observation_allowed=False,
        )

    insufficient: list[str] = []
    if evidence.closed_trade_count < active_policy.minimum_closed_trades_for_demo_observation:
        insufficient.append("CLOSED_TRADE_SAMPLE_TOO_SMALL")
    if evidence.observed_days < active_policy.minimum_observed_days_for_demo_observation:
        insufficient.append("OBSERVATION_WINDOW_TOO_SHORT")
    if insufficient:
        return CryptoEvidenceDecision(
            posture=CryptoResearchPosture.HOLD_SHADOW_INSUFFICIENT_SAMPLE,
            reasons=tuple(insufficient),
            demo_observation_allowed=False,
        )

    if active_policy.require_positive_net_pnl and evidence.total_net_pnl_usdt <= 0:
        return CryptoEvidenceDecision(
            posture=CryptoResearchPosture.RETUNE_NEGATIVE_ECONOMICS,
            reasons=("NON_POSITIVE_NET_PNL",),
            demo_observation_allowed=False,
        )
    if evidence.profit_factor is None or evidence.profit_factor < active_policy.minimum_profit_factor:
        return CryptoEvidenceDecision(
            posture=CryptoResearchPosture.RETUNE_NEGATIVE_ECONOMICS,
            reasons=("PROFIT_FACTOR_BELOW_MINIMUM",),
            demo_observation_allowed=False,
        )

    risk_reasons: list[str] = []
    if evidence.maximum_drawdown_pct > active_policy.maximum_drawdown_pct:
        risk_reasons.append("MAXIMUM_DRAWDOWN_TOO_HIGH")
    fee_ratio = evidence.fees_usdt / evidence.opening_equity_usdt
    if fee_ratio > active_policy.maximum_fees_to_opening_equity:
        risk_reasons.append("EXECUTION_COST_BURDEN_TOO_HIGH")
    if active_policy.require_zero_risk_budget_breaches and evidence.risk_budget_breach_count > 0:
        risk_reasons.append("RISK_BUDGET_BREACHES_PRESENT")
    if risk_reasons:
        return CryptoEvidenceDecision(
            posture=CryptoResearchPosture.RETUNE_RISK_QUALITY,
            reasons=tuple(risk_reasons),
            demo_observation_allowed=False,
        )

    return CryptoEvidenceDecision(
        posture=CryptoResearchPosture.ELIGIBLE_FOR_DEMO_OBSERVATION,
        reasons=("HISTORICAL_EVIDENCE_FLOOR_MET",),
        demo_observation_allowed=True,
        live_promotion_allowed=False,
    )
