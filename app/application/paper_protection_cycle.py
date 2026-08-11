from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from app.application.order_intents import order_intent_for_target
from app.application.order_lifecycle import PaperOrderLifecycle, PreparedPaperOrder
from app.application.paper_protection import (
    PaperProtectionDecision,
    PaperProtectionService,
    PaperProtectionState,
    PaperProtectionStatus,
)
from app.application.portfolio_paper_planner import (
    EntryExitGate,
    PortfolioPaperPlanItem,
    PortfolioPaperPlanner,
    prepare_approved_paper_orders,
)
from app.domain.trading import Side, TargetPosition
from app.risk.pretrade import RiskContext


@dataclass(frozen=True)
class PaperProtectionOrderResult:
    protection: PaperProtectionDecision
    plan_item: PortfolioPaperPlanItem | None
    prepared: PreparedPaperOrder | None
    existing_order_reused: bool


class PaperProtectionOrderService:
    """Fresh-price protection trigger through risk and durable paper OMS preparation.

    No broker call occurs here. Before a durable OMS record exists, a pending exit is
    repriced to the latest observed reference price so a prior risk rejection cannot
    strand a SELL at an obsolete limit. Once an OMS record exists, retries reuse the
    original intent even if the portfolio has since partially filled, preventing a
    second protective SELL from being created for the remaining quantity.
    """

    def __init__(
        self,
        *,
        protection: PaperProtectionService,
        planner: PortfolioPaperPlanner,
        lifecycle: PaperOrderLifecycle,
    ) -> None:
        if protection.ledger is not planner.ledger:
            raise ValueError("protection and planner must share one durable ledger")
        self.protection = protection
        self.planner = planner
        self.lifecycle = lifecycle

    def observe_and_prepare(
        self,
        *,
        symbol: str,
        reference_price: Decimal,
        observed_at: datetime,
        mark_prices: Mapping[str, Decimal],
        completed_bar_at: datetime | None = None,
        quality_gate: EntryExitGate | None = None,
        kill_switch_engaged: bool = False,
        risk_context: RiskContext | None = None,
    ) -> PaperProtectionOrderResult:
        decision = self.protection.observe(
            symbol=symbol,
            reference_price=reference_price,
            observed_at=observed_at,
            completed_bar_at=completed_bar_at,
        )
        if decision.status is not PaperProtectionStatus.EXIT_PENDING:
            return PaperProtectionOrderResult(
                protection=decision,
                plan_item=None,
                prepared=None,
                existing_order_reused=False,
            )
        if decision.exit_target is None or decision.trigger_quantity is None:
            raise RuntimeError("pending protection decision is missing exit identity")

        stable_intent = order_intent_for_target(
            decision.exit_target,
            current_quantity=decision.trigger_quantity,
        )
        if stable_intent is None or stable_intent.side is not Side.SELL:
            raise RuntimeError("paper protection must resolve to a SELL intent")
        existing = self.lifecycle.store.get(stable_intent.intent_id)
        if existing is not None:
            return PaperProtectionOrderResult(
                protection=decision,
                plan_item=None,
                prepared=PreparedPaperOrder(
                    record=existing,
                    client_order_id=existing.client_order_id,
                ),
                existing_order_reused=True,
            )

        current = self.planner.ledger.position(symbol)
        if current.quantity != decision.trigger_quantity:
            raise RuntimeError("PROTECTION_POSITION_CHANGED_BEFORE_DURABLE_OUTBOX")

        refreshed = self._refresh_unoutboxed_pending(
            decision,
            reference_price=reference_price,
            observed_at=observed_at,
        )
        target = refreshed.exit_target
        if target is None:
            raise RuntimeError("refreshed protection decision lost exit target")
        contexts = None if risk_context is None else {symbol: risk_context}
        plan = self.planner.plan(
            (target,),
            mark_prices=mark_prices,
            quality_gate=quality_gate,
            kill_switch_engaged=kill_switch_engaged,
            risk_contexts=contexts,
        )
        if len(plan.items) != 1:
            raise RuntimeError("single protection target produced invalid batch plan")
        item = plan.items[0]
        if not item.approved:
            return PaperProtectionOrderResult(
                protection=refreshed,
                plan_item=item,
                prepared=None,
                existing_order_reused=False,
            )
        prepared = prepare_approved_paper_orders(
            plan,
            lifecycle=self.lifecycle,
        )
        if len(prepared) != 1:
            raise RuntimeError("approved protection plan did not produce one paper order")
        return PaperProtectionOrderResult(
            protection=refreshed,
            plan_item=item,
            prepared=prepared[0],
            existing_order_reused=False,
        )

    def _refresh_unoutboxed_pending(
        self,
        decision: PaperProtectionDecision,
        *,
        reference_price: Decimal,
        observed_at: datetime,
    ) -> PaperProtectionDecision:
        state = decision.state
        if state is None or state.pending_exit_reason is None:
            raise RuntimeError("cannot refresh non-pending protection state")
        if (
            state.pending_target_price == reference_price
            and state.pending_target_created_at == observed_at
        ):
            return decision
        refreshed_state = replace(
            state,
            tracked_quantity=decision.trigger_quantity,
            average_cost=self.planner.ledger.position(state.symbol).average_cost,
            last_observed_at=observed_at,
            pending_target_price=reference_price,
            pending_target_created_at=observed_at,
        )
        refreshed_state.validate()
        self.protection.store.upsert(refreshed_state)
        return PaperProtectionDecision(
            status=PaperProtectionStatus.EXIT_PENDING,
            state=refreshed_state,
            exit_reason=refreshed_state.pending_exit_reason,
            exit_target=self._target(refreshed_state),
            trigger_quantity=refreshed_state.pending_trigger_quantity,
            protected_stop_price=decision.protected_stop_price,
            profit_fraction=decision.profit_fraction,
            maximum_favorable_excursion_fraction=(
                decision.maximum_favorable_excursion_fraction
            ),
        )

    def _target(self, state: PaperProtectionState) -> TargetPosition:
        if (
            state.pending_target_price is None
            or state.pending_target_created_at is None
        ):
            raise RuntimeError("pending protection state is missing target identity")
        return TargetPosition(
            symbol=state.symbol,
            quantity=Decimal("0"),
            reference_price=state.pending_target_price,
            generated_at=state.pending_target_created_at,
            strategy_id=f"{self.protection.strategy_id}:protection",
        )
