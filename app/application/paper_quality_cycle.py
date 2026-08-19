from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.application.cross_sectional_paper_cycle import CrossSectionalPaperCycleResult
from app.application.paper_candidate_shadow import (
    PaperCandidateShadowBatch,
    PaperCandidateShadowSuite,
)
from app.application.paper_decision_cycle import AuditedCrossSectionalPaperCycleService
from app.application.paper_execution_quality import SQLitePaperExecutionQualityStore
from app.application.paper_quality_gate import (
    ExecutionQualityGatePolicy,
    PaperQualityGateDecision,
    ReactionQualityGatePolicy,
    evaluate_paper_quality_gate,
)
from app.application.paper_reaction_quality import SQLitePaperReactionQualityStore
from app.marketdata.ohlcv import OhlcvBar
from app.risk.pretrade import RiskContext
from app.strategy.cross_sectional_portfolio import (
    PortfolioEntryBlockReason,
    PortfolioExitReason,
)
from app.strategy.quality_monitor import (
    StrategyQualityGateDecision,
    TradeQualityMonitorPolicy,
)


class TradeQualityGateProvider(Protocol):
    strategy_id: str

    def quality_gate(
        self,
        *,
        policy: TradeQualityMonitorPolicy,
    ) -> StrategyQualityGateDecision: ...


@dataclass(frozen=True)
class PaperQualityManagedCycleResult:
    quality_gate: PaperQualityGateDecision
    decision: CrossSectionalPaperCycleResult
    candidate_shadow: PaperCandidateShadowBatch | None = None


class QualityManagedCrossSectionalPaperCycleService:
    """Derive current paper health before every cross-sectional decision.

    Trade outcome, execution slippage and reaction latency evidence are composed before
    the audited baseline strategy cycle. An optional candidate-shadow suite is invoked
    only after the baseline decision has already produced its durable OMS outbox, so
    counterfactual research cannot alter, delay or cancel actual paper order intents.
    Observer failures are returned as evidence failures instead of raising through the
    execution path. The quality gate may pause new BUYs but always keeps exits enabled.
    """

    def __init__(
        self,
        *,
        cycle: AuditedCrossSectionalPaperCycleService,
        trade_quality: TradeQualityGateProvider,
        trade_policy: TradeQualityMonitorPolicy,
        execution_store: SQLitePaperExecutionQualityStore | None = None,
        execution_policy: ExecutionQualityGatePolicy | None = None,
        reaction_store: SQLitePaperReactionQualityStore | None = None,
        reaction_policy: ReactionQualityGatePolicy | None = None,
        candidate_shadow: PaperCandidateShadowSuite | None = None,
    ) -> None:
        strategy_id = cycle.cycle.target_planner.strategy_id
        if trade_quality.strategy_id != strategy_id:
            raise ValueError("trade quality and paper cycle must share strategy_id")
        trade_policy.validate()
        if (execution_store is None) != (execution_policy is None):
            raise ValueError("execution store and policy must be supplied together")
        if (reaction_store is None) != (reaction_policy is None):
            raise ValueError("reaction store and policy must be supplied together")
        if execution_policy is not None:
            execution_policy.validate()
        if reaction_policy is not None:
            reaction_policy.validate()
        self.cycle = cycle
        self.trade_quality = trade_quality
        self.trade_policy = trade_policy
        self.execution_store = execution_store
        self.execution_policy = execution_policy
        self.reaction_store = reaction_store
        self.reaction_policy = reaction_policy
        self.candidate_shadow = candidate_shadow
        self.strategy_id = strategy_id

    def current_gate(self) -> PaperQualityGateDecision:
        trade_gate = self.trade_quality.quality_gate(policy=self.trade_policy)
        return evaluate_paper_quality_gate(
            trade_gate=trade_gate,
            execution_store=self.execution_store,
            execution_policy=self.execution_policy,
            reaction_store=self.reaction_store,
            reaction_policy=self.reaction_policy,
            strategy_id=self.strategy_id,
        )

    def plan_and_prepare(
        self,
        bars: Iterable[OhlcvBar],
        *,
        reference_prices: Mapping[str, Decimal],
        generated_at: datetime,
        kill_switch_engaged: bool = False,
        risk_contexts: Mapping[str, RiskContext] | None = None,
        blocked_entries: Mapping[str, PortfolioEntryBlockReason] | None = None,
        protective_exits: Mapping[str, PortfolioExitReason] | None = None,
    ) -> PaperQualityManagedCycleResult:
        materialized = tuple(bars)
        gate = self.current_gate()
        decision = self.cycle.plan_and_prepare(
            materialized,
            reference_prices=reference_prices,
            generated_at=generated_at,
            quality_gate=gate,
            kill_switch_engaged=kill_switch_engaged,
            risk_contexts=risk_contexts,
            blocked_entries=blocked_entries,
            protective_exits=protective_exits,
        )
        shadow = (
            None
            if self.candidate_shadow is None
            else self.candidate_shadow.observe(
                materialized,
                baseline_plan=decision.target_plan,
                observed_at=generated_at,
            )
        )
        return PaperQualityManagedCycleResult(
            quality_gate=gate,
            decision=decision,
            candidate_shadow=shadow,
        )
