"""Non-vacuous research eligibility gate; never grants broker/runtime promotion.

Legacy v1 qualification output remains unchanged for frozen historical evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.strategy.qualification import StrategyQualification


@dataclass(frozen=True)
class ResearchEligibilityPolicyV2:
    minimum_trades: int = 10
    minimum_active_windows: int = 3
    minimum_positive_return: Decimal = Decimal("0")
    minimum_excess_return: Decimal = Decimal("0")
    maximum_drawdown_fraction: Decimal = Decimal("0.10")

    def validate(self) -> None:
        if self.minimum_trades < 1 or self.minimum_active_windows < 1:
            raise ValueError("research eligibility sample floors must be positive")
        for value in (
            self.minimum_positive_return,
            self.minimum_excess_return,
            self.maximum_drawdown_fraction,
        ):
            if not value.is_finite():
                raise ValueError("eligibility thresholds must be finite")
        if not Decimal("0") <= self.maximum_drawdown_fraction <= Decimal("1"):
            raise ValueError("drawdown threshold outside [0, 1]")


@dataclass(frozen=True)
class ResearchEligibilityV2:
    eligible_for_further_research: bool
    reasons: tuple[str, ...]
    total_trades: int
    active_windows: int
    runtime_promotion_allowed: bool = False
    live_trading_allowed: bool = False


def assess_research_eligibility(
    qualification: StrategyQualification,
    *,
    policy: ResearchEligibilityPolicyV2 | None = None,
) -> ResearchEligibilityV2:
    """Evaluate historical coverage; promotion ALWAYS requires separate authority."""
    selected = policy or ResearchEligibilityPolicyV2()
    selected.validate()
    reasons: set[str] = set()
    if not qualification.windows:
        reasons.add("NO_WALK_FORWARD_WINDOWS")
    if not qualification.qualified:
        reasons.add("LEGACY_QUALIFICATION_FAILED")
    if qualification.total_trades < selected.minimum_trades:
        reasons.add("INSUFFICIENT_CLOSED_TRADES")
    if qualification.active_windows < selected.minimum_active_windows:
        reasons.add("INSUFFICIENT_ACTIVE_WINDOWS")
    if qualification.mean_oos_return <= selected.minimum_positive_return:
        reasons.add("NONPOSITIVE_MEAN_RETURN")
    if qualification.mean_excess_return <= selected.minimum_excess_return:
        reasons.add("NONPOSITIVE_MEAN_EXCESS_RETURN")
    if qualification.worst_drawdown_fraction > selected.maximum_drawdown_fraction:
        reasons.add("DRAWDOWN_THRESHOLD_EXCEEDED")
    return ResearchEligibilityV2(
        eligible_for_further_research=not reasons,
        reasons=tuple(sorted(reasons)),
        total_trades=qualification.total_trades,
        active_windows=qualification.active_windows,
    )
