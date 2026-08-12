from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.application.paper_candidate_shadow import PaperCandidateShadowSuite
from app.application.paper_quality_cycle import (
    QualityManagedCrossSectionalPaperCycleService,
)
from app.strategy.quality_monitor import (
    StrategyQualityGateDecision,
    StrategyQualityStatus,
    TradeQualityMonitorPolicy,
    TradeQualityWindow,
)

NOW = datetime(2026, 8, 12, 19, 0, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


def policy() -> TradeQualityMonitorPolicy:
    return TradeQualityMonitorPolicy(
        window_trades=20,
        minimum_observations=10,
        minimum_profit_factor=Decimal("1"),
        minimum_profit_preservation_rate=Decimal("0.5"),
        minimum_average_mfe_capture_ratio=Decimal("0.1"),
        maximum_hard_stop_fraction=Decimal("0.5"),
        maximum_consecutive_losses=4,
        allow_entries_when_insufficient_data=False,
    )


def healthy_gate() -> StrategyQualityGateDecision:
    return StrategyQualityGateDecision(
        status=StrategyQualityStatus.HEALTHY,
        allow_new_entries=True,
        allow_exits=True,
        reasons=(),
        metrics=TradeQualityWindow(
            observation_count=20,
            winning_trades=12,
            losing_trades=8,
            breakeven_trades=0,
            gross_profit=Decimal("120"),
            gross_loss=Decimal("-60"),
            total_pnl=Decimal("60"),
            win_rate=Decimal("0.6"),
            profit_factor=Decimal("2"),
            positive_mfe_trades=18,
            positive_mfe_closed_profitable=12,
            profit_preservation_rate=Decimal("0.6666666667"),
            average_mfe_capture_ratio=Decimal("0.45"),
            hard_stop_fraction=Decimal("0.2"),
            current_consecutive_losses=0,
        ),
    )


class TradeQuality:
    strategy_id = STRATEGY

    def quality_gate(self, *, policy):
        del policy
        return healthy_gate()


class AuditedCycle:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.cycle = SimpleNamespace(
            target_planner=SimpleNamespace(strategy_id=STRATEGY)
        )

    def plan_and_prepare(self, bars, **kwargs):
        del bars, kwargs
        self.events.append("baseline")
        return SimpleNamespace(
            target_plan=SimpleNamespace(
                decision_time=NOW,
                selected_symbols=(),
                exit_reasons=(),
            ),
            prepared_orders=(SimpleNamespace(record=SimpleNamespace(intent_id="oms-1")),),
        )


class FailingObserver:
    name = "BROKEN_COUNTERFACTUAL"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def observe(self, bars, *, baseline_plan, observed_at):
        del bars, baseline_plan, observed_at
        self.events.append("shadow")
        raise RuntimeError("candidate failure")


def test_shadow_failure_cannot_cancel_completed_baseline_decision() -> None:
    events: list[str] = []
    service = QualityManagedCrossSectionalPaperCycleService(
        cycle=AuditedCycle(events),
        trade_quality=TradeQuality(),
        trade_policy=policy(),
        candidate_shadow=PaperCandidateShadowSuite((FailingObserver(events),)),
    )

    result = service.plan_and_prepare(
        (),
        reference_prices={},
        generated_at=NOW,
    )

    assert events == ["baseline", "shadow"]
    assert result.decision.prepared_orders[0].record.intent_id == "oms-1"
    assert result.candidate_shadow is not None
    assert result.candidate_shadow.records == ()
    assert len(result.candidate_shadow.failures) == 1
    assert result.candidate_shadow.failures[0].observer == "BROKEN_COUNTERFACTUAL"
