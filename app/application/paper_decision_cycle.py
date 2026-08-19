from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal

from app.application.cross_sectional_paper_cycle import (
    CrossSectionalPaperCycleResult,
    CrossSectionalPaperCycleService,
)
from app.application.paper_decision_audit import (
    SQLitePaperDecisionAuditStore,
    audit_cross_sectional_paper_result,
)
from app.application.portfolio_paper_planner import EntryExitGate
from app.marketdata.ohlcv import OhlcvBar
from app.risk.pretrade import RiskContext
from app.strategy.cross_sectional_portfolio import (
    PortfolioEntryBlockReason,
    PortfolioExitReason,
)


class AuditedCrossSectionalPaperCycleService:
    """Persist the exact selection, health, target and risk rationale per decision."""

    def __init__(
        self,
        *,
        cycle: CrossSectionalPaperCycleService,
        audit_store: SQLitePaperDecisionAuditStore,
    ) -> None:
        self.cycle = cycle
        self.audit_store = audit_store

    def plan_and_prepare(
        self,
        bars: Iterable[OhlcvBar],
        *,
        reference_prices: Mapping[str, Decimal],
        generated_at: datetime,
        quality_gate: EntryExitGate | None = None,
        kill_switch_engaged: bool = False,
        risk_contexts: Mapping[str, RiskContext] | None = None,
        blocked_entries: Mapping[str, PortfolioEntryBlockReason] | None = None,
        protective_exits: Mapping[str, PortfolioExitReason] | None = None,
    ) -> CrossSectionalPaperCycleResult:
        result = self.cycle.plan_and_prepare(
            bars,
            reference_prices=reference_prices,
            generated_at=generated_at,
            quality_gate=quality_gate,
            kill_switch_engaged=kill_switch_engaged,
            risk_contexts=risk_contexts,
            blocked_entries=blocked_entries,
            protective_exits=protective_exits,
        )
        audit_cross_sectional_paper_result(
            store=self.audit_store,
            strategy_id=self.cycle.target_planner.strategy_id,
            generated_at=generated_at,
            result=result,
            quality_gate=quality_gate,
        )
        return result
