from dataclasses import replace
from decimal import Decimal

import pytest

from app.strategy.research_eligibility_v2 import (
    ResearchEligibilityPolicyV2,
    assess_research_eligibility,
)
from tests.test_strategy_qualification import qualifier, series


def test_legacy_zero_trade_success_is_rejected_by_v2() -> None:
    legacy = qualifier().qualify(series(list(range(130, 100, -1))))
    assert legacy.qualified and legacy.total_trades == 0
    decision = assess_research_eligibility(legacy)
    assert not decision.eligible_for_further_research
    assert "INSUFFICIENT_CLOSED_TRADES" in decision.reasons
    assert "INSUFFICIENT_ACTIVE_WINDOWS" in decision.reasons
    assert decision.runtime_promotion_allowed is False
    assert decision.live_trading_allowed is False


def test_positive_activity_still_requires_positive_edge() -> None:
    legacy = qualifier().qualify(series(list(range(100, 130))))
    decision = assess_research_eligibility(legacy)
    assert decision.runtime_promotion_allowed is False
    assert decision.live_trading_allowed is False
    if decision.eligible_for_further_research:
        assert legacy.total_trades >= 10
        assert legacy.mean_excess_return > 0


def test_no_history_cannot_qualify() -> None:
    legacy = qualifier().qualify([])
    decision = assess_research_eligibility(legacy)
    assert not decision.eligible_for_further_research
    assert "NO_WALK_FORWARD_WINDOWS" in decision.reasons


def test_drawdown_threshold_fails_closed() -> None:
    legacy = qualifier().qualify(series(list(range(100, 130))))
    adverse = replace(legacy, worst_drawdown_fraction=Decimal("0.30"))
    decision = assess_research_eligibility(adverse)
    assert "DRAWDOWN_THRESHOLD_EXCEEDED" in decision.reasons


def test_invalid_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        ResearchEligibilityPolicyV2(minimum_trades=0).validate()
