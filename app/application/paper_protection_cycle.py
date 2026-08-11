from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.application.order_intents import order_intent_for_target
from app.application.order_lifecycle import PaperOrderLifecycle, PreparedPaperOrder
from app.application.paper_protection import (
    PaperProtectionDecision,
    PaperProtectionService,
    PaperProtectionState,
    PaperProtectionStatus,
)
from app.application.paper_protection_reprice import (
    PaperProtectionRepricePlanner,
    ProtectiveRepriceDecision,
)
from app.application.portfolio_paper_planner import (
    EntryExitGate,
    PortfolioPaperPlanItem,
    PortfolioPaperPlanner,
    prepare_approved_paper_orders,
)
from app.domain.trading import Side, TargetPosition
from app.oms.store import OrderState
from app.risk.pretrade import RiskContext


class ProtectionQualityRecorder(Protocol):
    def observe_price(
        self,
        *,
        symbol: str,
        reference_price: Decimal,
        observed_at: datetime,
    ) -> object | None: ...

    def register_exit_intent(
        self,
        *,
        intent_id: str,
        symbol: str,
        exit_reason: str,
        registered_at: datetime,
    ) -> None: ...


@dataclass(frozen=True)
class PaperProtectionOrderResult:
    protection: PaperProtectionDecision
    plan_item: PortfolioPaperPlanItem | None
    prepared: PreparedPaperOrder | None
    existing_order_reused: bool
    reprice: ProtectiveRepriceDecision | None = None


class PaperProtectionOrderService:
    """Fresh-price protection trigger through risk and durable paper OMS preparation.

    No broker call occurs here. Before a durable OMS record exists, a pending exit is
    repriced to the latest observed reference price so a prior risk rejection cannot
    strand a SELL at an obsolete limit. Once an active broker order exists, an optional
    reprice planner can write an adverse-only durable REPLACE mutation instead of
    creating a second SELL. Cancelled/rejected terminal attempts may be reissued for the
    verified remaining quantity on a newer price observation.

    When a strategy-scoped quality recorder is supplied, the same fresh prices update
    observed MFE/MAE and an approved protective exit reason is durably registered before
    OMS outbox creation. Registration also runs on an existing-order retry so it can
    heal an older crash or deployment window without creating another SELL.
    """

    _RECOVERABLE_PREOUTBOX = frozenset(
        {OrderState.CREATED, OrderState.RISK_APPROVED}
    )
    _REISSUABLE_TERMINAL = frozenset({OrderState.CANCELLED, OrderState.REJECTED})

    def __init__(
        self,
        *,
        protection: PaperProtectionService,
        planner: PortfolioPaperPlanner,
        lifecycle: PaperOrderLifecycle,
        quality_recorder: ProtectionQualityRecorder | None = None,
        reprice_planner: PaperProtectionRepricePlanner | None = None,
    ) -> None:
        if protection.ledger is not planner.ledger:
            raise ValueError("protection and planner must share one durable ledger")
        if reprice_planner is not None and reprice_planner.oms is not lifecycle.store:
            raise ValueError("reprice planner and order lifecycle must share one OMS")
        self.protection = protection
        self.planner = planner
        self.lifecycle = lifecycle
        self.quality_recorder = quality_recorder
        self.reprice_planner = reprice_planner

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
        if self.quality_recorder is not None:
            self.quality_recorder.observe_price(
                symbol=symbol,
                reference_price=reference_price,
                observed_at=observed_at,
            )
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
            if existing.state is OrderState.FILLED:
                raise RuntimeError(
                    "PROTECTION_FILLED_ORDER_AWAITING_LEDGER_RECONCILIATION"
                )
            if existing.state in self._RECOVERABLE_PREOUTBOX:
                current = self.planner.ledger.position(symbol)
                if current.quantity != decision.trigger_quantity:
                    raise RuntimeError(
                        "PROTECTION_POSITION_CHANGED_BEFORE_DURABLE_OUTBOX"
                    )
                return self._plan_and_prepare_target(
                    decision=decision,
                    target=decision.exit_target,
                    mark_prices=mark_prices,
                    quality_gate=quality_gate,
                    kill_switch_engaged=kill_switch_engaged,
                    risk_context=risk_context,
                    existing_order_reused=True,
                )
            if existing.state not in self._REISSUABLE_TERMINAL:
                self._register_exit(
                    intent_id=stable_intent.intent_id,
                    target=decision.exit_target,
                    decision=decision,
                )
                reprice = (
                    None
                    if self.reprice_planner is None
                    else self.reprice_planner.consider(
                        intent_id=stable_intent.intent_id,
                        reference_price=reference_price,
                        observed_at=observed_at,
                    )
                )
                return PaperProtectionOrderResult(
                    protection=decision,
                    plan_item=None,
                    prepared=PreparedPaperOrder(
                        record=existing,
                        client_order_id=existing.client_order_id,
                    ),
                    existing_order_reused=True,
                    reprice=reprice,
                )

            current = self.planner.ledger.position(symbol)
            if current.quantity <= 0:
                raise RuntimeError("terminal protective order has no remaining position")
            if current.quantity > decision.trigger_quantity:
                raise RuntimeError("PROTECTION_POSITION_INCREASED_DURING_EXIT")
            refreshed = self._refresh_unoutboxed_pending(
                decision,
                reference_price=reference_price,
                observed_at=observed_at,
                trigger_quantity=current.quantity,
            )
            new_target = refreshed.exit_target
            if new_target is None:
                raise RuntimeError("refreshed terminal protection lost exit target")
            replacement_intent = order_intent_for_target(
                new_target,
                current_quantity=current.quantity,
            )
            if (
                replacement_intent is None
                or replacement_intent.intent_id == stable_intent.intent_id
            ):
                raise RuntimeError(
                    "PROTECTION_TERMINAL_ORDER_REQUIRES_NEW_OBSERVATION"
                )
            return self._plan_and_prepare_target(
                decision=refreshed,
                target=new_target,
                mark_prices=mark_prices,
                quality_gate=quality_gate,
                kill_switch_engaged=kill_switch_engaged,
                risk_context=risk_context,
                existing_order_reused=False,
            )

        current = self.planner.ledger.position(symbol)
        if current.quantity != decision.trigger_quantity:
            raise RuntimeError("PROTECTION_POSITION_CHANGED_BEFORE_DURABLE_OUTBOX")
        refreshed = self._refresh_unoutboxed_pending(
            decision,
            reference_price=reference_price,
            observed_at=observed_at,
            trigger_quantity=current.quantity,
        )
        target = refreshed.exit_target
        if target is None:
            raise RuntimeError("refreshed protection decision lost exit target")
        return self._plan_and_prepare_target(
            decision=refreshed,
            target=target,
            mark_prices=mark_prices,
            quality_gate=quality_gate,
            kill_switch_engaged=kill_switch_engaged,
            risk_context=risk_context,
            existing_order_reused=False,
        )

    def _plan_and_prepare_target(
        self,
        *,
        decision: PaperProtectionDecision,
        target: TargetPosition,
        mark_prices: Mapping[str, Decimal],
        quality_gate: EntryExitGate | None,
        kill_switch_engaged: bool,
        risk_context: RiskContext | None,
        existing_order_reused: bool,
    ) -> PaperProtectionOrderResult:
        contexts = None if risk_context is None else {target.symbol: risk_context}
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
                protection=decision,
                plan_item=item,
                prepared=None,
                existing_order_reused=existing_order_reused,
            )
        if item.intent is None:
            raise RuntimeError("approved protection item is missing SELL intent")
        self._register_exit(
            intent_id=item.intent.intent_id,
            target=item.target,
            decision=decision,
        )
        prepared = prepare_approved_paper_orders(
            plan,
            lifecycle=self.lifecycle,
        )
        if len(prepared) != 1:
            raise RuntimeError("approved protection plan did not produce one paper order")
        return PaperProtectionOrderResult(
            protection=decision,
            plan_item=item,
            prepared=prepared[0],
            existing_order_reused=existing_order_reused,
        )

    def _register_exit(
        self,
        *,
        intent_id: str,
        target: TargetPosition,
        decision: PaperProtectionDecision,
    ) -> None:
        if self.quality_recorder is None:
            return
        if decision.exit_reason is None:
            raise RuntimeError("pending protection exit is missing reason")
        self.quality_recorder.register_exit_intent(
            intent_id=intent_id,
            symbol=target.symbol,
            exit_reason=decision.exit_reason.value,
            registered_at=target.generated_at,
        )

    def _refresh_unoutboxed_pending(
        self,
        decision: PaperProtectionDecision,
        *,
        reference_price: Decimal,
        observed_at: datetime,
        trigger_quantity: Decimal,
    ) -> PaperProtectionDecision:
        state = decision.state
        if state is None or state.pending_exit_reason is None:
            raise RuntimeError("cannot refresh non-pending protection state")
        if (
            state.pending_target_price == reference_price
            and state.pending_target_created_at == observed_at
            and state.pending_trigger_quantity == trigger_quantity
        ):
            return decision
        refreshed_state = replace(
            state,
            tracked_quantity=trigger_quantity,
            average_cost=self.planner.ledger.position(state.symbol).average_cost,
            last_observed_at=observed_at,
            pending_target_price=reference_price,
            pending_target_created_at=observed_at,
            pending_trigger_quantity=trigger_quantity,
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
