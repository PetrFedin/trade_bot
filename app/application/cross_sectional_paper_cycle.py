from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.application.cross_sectional_target_planner import (
    CrossSectionalTargetPlan,
    CrossSectionalTargetPlanner,
)
from app.application.order_lifecycle import PaperOrderLifecycle, PreparedPaperOrder
from app.application.portfolio_paper_planner import (
    EntryExitGate,
    PortfolioPaperPlan,
    PortfolioPaperPlanner,
    prepare_approved_paper_orders,
)
from app.marketdata.ohlcv import OhlcvBar
from app.risk.pretrade import RiskContext
from app.strategy.cross_sectional_portfolio import (
    PortfolioEntryBlockReason,
    PortfolioExitReason,
)


class ExitQualityRecorder(Protocol):
    strategy_id: str

    def register_exit_intent(
        self,
        *,
        intent_id: str,
        symbol: str,
        exit_reason: str,
        registered_at: datetime,
    ) -> None: ...


@dataclass(frozen=True)
class CrossSectionalPaperCycleResult:
    target_plan: CrossSectionalTargetPlan
    order_plan: PortfolioPaperPlan
    prepared_orders: tuple[PreparedPaperOrder, ...]

    @property
    def prepared_exit_count(self) -> int:
        return self.order_plan.approved_exit_count

    @property
    def prepared_entry_count(self) -> int:
        return self.order_plan.approved_entry_count


class CrossSectionalPaperCycleService:
    """Prepare one cross-sectional strategy decision into a durable paper outbox.

    This is intentionally the last boundary before broker submission. It performs no
    external broker mutation. Selection, durable re-entry blocks, sizing and strategy
    gross admission are resolved by the target planner; batch cash/gross/risk controls
    are resolved by the portfolio paper planner. Approved exit reasons are registered
    before OMS outbox persistence so exact fills can later produce attributed paper
    trade-quality observations.
    """

    def __init__(
        self,
        *,
        target_planner: CrossSectionalTargetPlanner,
        order_planner: PortfolioPaperPlanner,
        lifecycle: PaperOrderLifecycle,
        quality_recorder: ExitQualityRecorder | None = None,
    ) -> None:
        if (
            quality_recorder is not None
            and quality_recorder.strategy_id != target_planner.strategy_id
        ):
            raise ValueError(
                "quality recorder and target planner must share one strategy_id"
            )
        self.target_planner = target_planner
        self.order_planner = order_planner
        self.lifecycle = lifecycle
        self.quality_recorder = quality_recorder

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
        target_plan = self.target_planner.plan(
            bars,
            ledger=self.order_planner.ledger,
            reference_prices=reference_prices,
            generated_at=generated_at,
            blocked_entries=blocked_entries,
            protective_exits=protective_exits,
        )
        order_plan = self.order_planner.plan(
            target_plan.targets,
            mark_prices=reference_prices,
            quality_gate=quality_gate,
            kill_switch_engaged=kill_switch_engaged,
            risk_contexts=risk_contexts,
        )
        self._register_approved_exit_reasons(
            target_plan=target_plan,
            order_plan=order_plan,
        )
        prepared = prepare_approved_paper_orders(
            order_plan,
            lifecycle=self.lifecycle,
        )
        return CrossSectionalPaperCycleResult(
            target_plan=target_plan,
            order_plan=order_plan,
            prepared_orders=prepared,
        )

    def _register_approved_exit_reasons(
        self,
        *,
        target_plan: CrossSectionalTargetPlan,
        order_plan: PortfolioPaperPlan,
    ) -> None:
        if self.quality_recorder is None:
            return
        reasons = dict(target_plan.exit_reasons)
        for item in order_plan.approved_items:
            if item.intent is None or item.intent.side.value != "SELL":
                continue
            reason = reasons.get(item.target.symbol)
            if reason is None:
                raise RuntimeError(
                    f"approved strategy exit missing reason:{item.target.symbol}"
                )
            self.quality_recorder.register_exit_intent(
                intent_id=item.intent.intent_id,
                symbol=item.target.symbol,
                exit_reason=reason.value,
                registered_at=item.target.generated_at,
            )
