from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.application.order_intents import order_intent_for_target
from app.application.order_lifecycle import PaperOrderLifecycle, PreparedPaperOrder
from app.domain.trading import OrderIntent, Side, TargetPosition
from app.portfolio.ledger import PortfolioLedger
from app.risk.evidence import RecordedRiskDecision, RiskAdmissionService
from app.risk.pretrade import PreTradeRiskEngine, RiskContext, RiskDecision


class EntryExitGate(Protocol):
    allow_new_entries: bool
    allow_exits: bool
    reasons: tuple[str, ...]


class PortfolioPaperDisposition(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    ENTRY_PAUSED = "ENTRY_PAUSED"
    RISK_REJECTED = "RISK_REJECTED"
    RISK_APPROVED = "RISK_APPROVED"


@dataclass(frozen=True)
class PortfolioPaperPlanItem:
    target: TargetPosition
    current_quantity: Decimal
    intent: OrderIntent | None
    risk: RiskDecision | None
    recorded_risk: RecordedRiskDecision | None
    disposition: PortfolioPaperDisposition
    reasons: tuple[str, ...]

    @property
    def approved(self) -> bool:
        return self.disposition is PortfolioPaperDisposition.RISK_APPROVED


@dataclass(frozen=True)
class PortfolioPaperPlan:
    items: tuple[PortfolioPaperPlanItem, ...]
    starting_cash: Decimal
    starting_gross_notional: Decimal
    reserved_buy_notional: Decimal
    reserved_turnover_notional: Decimal

    @property
    def approved_items(self) -> tuple[PortfolioPaperPlanItem, ...]:
        return tuple(item for item in self.items if item.approved)

    @property
    def approved_exit_count(self) -> int:
        return sum(
            item.approved
            and item.intent is not None
            and item.intent.side is Side.SELL
            for item in self.items
        )

    @property
    def approved_entry_count(self) -> int:
        return sum(
            item.approved
            and item.intent is not None
            and item.intent.side is Side.BUY
            for item in self.items
        )


class PortfolioPaperPlanner:
    """Conservative multi-symbol planner for durable paper order preparation.

    Targets are delta-converted using the same deterministic intent factory as the
    stable single-symbol pipeline. SELL intents are evaluated before BUY intents.
    Entry-quality gating applies only to BUYs; exits are never allowed to be blocked
    by that gate.

    Approved SELLs do not release hypothetical cash or gross exposure for subsequent
    BUY admission because those exits may remain unfilled. Approved BUY notionals are
    reserved against later BUYs, preventing a batch from independently passing cash or
    gross limits and then overcommitting the durable portfolio.
    """

    def __init__(
        self,
        *,
        ledger: PortfolioLedger,
        risk: PreTradeRiskEngine,
        risk_admission: RiskAdmissionService | None = None,
    ) -> None:
        if risk_admission is not None and risk_admission.engine is not risk:
            raise ValueError("risk_admission must use the planner risk engine")
        self.ledger = ledger
        self.risk = risk
        self.risk_admission = risk_admission

    def plan(
        self,
        targets: Sequence[TargetPosition],
        *,
        mark_prices: Mapping[str, Decimal],
        quality_gate: EntryExitGate | None = None,
        kill_switch_engaged: bool = False,
        risk_contexts: Mapping[str, RiskContext] | None = None,
    ) -> PortfolioPaperPlan:
        materialized = tuple(targets)
        self._validate_targets(materialized)
        marks = self._mark_prices(mark_prices, materialized)
        starting_gross = self.ledger.gross_notional(marks)
        contexts = {} if risk_contexts is None else dict(risk_contexts)

        raw: list[tuple[int, TargetPosition, Decimal, OrderIntent | None]] = []
        for index, target in enumerate(materialized):
            current_quantity = self.ledger.position(target.symbol).quantity
            intent = order_intent_for_target(
                target,
                current_quantity=current_quantity,
            )
            raw.append((index, target, current_quantity, intent))

        ordered = sorted(raw, key=self._execution_priority)
        items: list[PortfolioPaperPlanItem] = []
        reserved_buy = Decimal("0")
        reserved_turnover = Decimal("0")

        for _, target, current_quantity, intent in ordered:
            if intent is None:
                items.append(
                    PortfolioPaperPlanItem(
                        target=target,
                        current_quantity=current_quantity,
                        intent=None,
                        risk=None,
                        recorded_risk=None,
                        disposition=PortfolioPaperDisposition.NO_CHANGE,
                        reasons=(),
                    )
                )
                continue

            if intent.side is Side.SELL and quality_gate is not None:
                if not quality_gate.allow_exits:
                    raise ValueError("QUALITY_GATE_MUST_NOT_BLOCK_EXITS")
            if (
                intent.side is Side.BUY
                and quality_gate is not None
                and not quality_gate.allow_new_entries
            ):
                reasons = tuple(
                    dict.fromkeys(("QUALITY_GATE_PAUSE_ENTRIES", *quality_gate.reasons))
                )
                items.append(
                    PortfolioPaperPlanItem(
                        target=target,
                        current_quantity=current_quantity,
                        intent=intent,
                        risk=None,
                        recorded_risk=None,
                        disposition=PortfolioPaperDisposition.ENTRY_PAUSED,
                        reasons=reasons,
                    )
                )
                continue

            available_cash = self.ledger.cash - reserved_buy
            if available_cash < 0:
                raise RuntimeError("reserved buy notional exceeded durable cash")
            context = self._risk_context(
                target=target,
                supplied=contexts.get(target.symbol),
                available_cash=available_cash,
                reserved_turnover=reserved_turnover,
            )
            current_symbol_notional = current_quantity * target.reference_price
            admission_gross = starting_gross + reserved_buy
            recorded: RecordedRiskDecision | None = None
            if self.risk_admission is None:
                decision = self.risk.evaluate(
                    intent,
                    current_symbol_notional=current_symbol_notional,
                    current_gross_notional=admission_gross,
                    kill_switch_engaged=kill_switch_engaged,
                    context=context,
                )
            else:
                recorded = self.risk_admission.evaluate_and_record(
                    intent,
                    current_symbol_notional=current_symbol_notional,
                    current_gross_notional=admission_gross,
                    kill_switch_engaged=kill_switch_engaged,
                    context=context,
                    evaluated_at=target.generated_at,
                )
                decision = recorded.decision

            disposition = (
                PortfolioPaperDisposition.RISK_APPROVED
                if decision.approved
                else PortfolioPaperDisposition.RISK_REJECTED
            )
            items.append(
                PortfolioPaperPlanItem(
                    target=target,
                    current_quantity=current_quantity,
                    intent=intent,
                    risk=decision,
                    recorded_risk=recorded,
                    disposition=disposition,
                    reasons=decision.reasons,
                )
            )
            if decision.approved:
                reserved_turnover += decision.order_notional
                if intent.side is Side.BUY:
                    reserved_buy += decision.order_notional

        return PortfolioPaperPlan(
            items=tuple(items),
            starting_cash=self.ledger.cash,
            starting_gross_notional=starting_gross,
            reserved_buy_notional=reserved_buy,
            reserved_turnover_notional=reserved_turnover,
        )

    @staticmethod
    def _execution_priority(
        item: tuple[int, TargetPosition, Decimal, OrderIntent | None],
    ) -> tuple[int, int]:
        index, _, _, intent = item
        if intent is not None and intent.side is Side.SELL:
            return (0, index)
        if intent is not None and intent.side is Side.BUY:
            return (1, index)
        return (2, index)

    def _risk_context(
        self,
        *,
        target: TargetPosition,
        supplied: RiskContext | None,
        available_cash: Decimal,
        reserved_turnover: Decimal,
    ) -> RiskContext:
        if supplied is None:
            return RiskContext(
                price_timestamp=target.generated_at,
                decision_time=target.generated_at,
                turnover_notional=reserved_turnover,
                available_cash=available_cash,
            )
        if supplied.available_cash not in (None, self.ledger.cash):
            raise ValueError(
                "risk_context available_cash disagrees with durable portfolio cash"
            )
        return replace(
            supplied,
            turnover_notional=supplied.turnover_notional + reserved_turnover,
            available_cash=available_cash,
        )

    def _mark_prices(
        self,
        mark_prices: Mapping[str, Decimal],
        targets: tuple[TargetPosition, ...],
    ) -> dict[str, Decimal]:
        marks: dict[str, Decimal] = {}
        for symbol, price in mark_prices.items():
            if not symbol or symbol != symbol.strip().upper():
                raise ValueError("mark price symbols must be normalized uppercase")
            if not price.is_finite() or price <= 0:
                raise ValueError(f"valid mark price required for {symbol}")
            marks[symbol] = price
        for target in targets:
            marks.setdefault(target.symbol, target.reference_price)
        for position in self.ledger.positions():
            if position.quantity > 0 and position.symbol not in marks:
                raise ValueError(f"valid mark price required for {position.symbol}")
        return marks

    @staticmethod
    def _validate_targets(targets: tuple[TargetPosition, ...]) -> None:
        seen: set[str] = set()
        generated_at = None
        for target in targets:
            target.validate()
            if target.symbol in seen:
                raise ValueError(f"duplicate portfolio target for {target.symbol}")
            seen.add(target.symbol)
            if generated_at is None:
                generated_at = target.generated_at
            elif target.generated_at != generated_at:
                raise ValueError("portfolio targets must share one decision timestamp")


def prepare_approved_paper_orders(
    plan: PortfolioPaperPlan,
    *,
    lifecycle: PaperOrderLifecycle,
) -> tuple[PreparedPaperOrder, ...]:
    """Persist approved plan items into the durable paper submit outbox.

    This function does not call a broker. Plan ordering is preserved, so approved
    exits are outboxed before approved entries.
    """

    prepared: list[PreparedPaperOrder] = []
    for item in plan.approved_items:
        if item.intent is None or item.risk is None or not item.risk.approved:
            raise RuntimeError("approved portfolio plan item is internally inconsistent")
        prepared.append(
            lifecycle.prepare(
                item.intent,
                item.risk,
                occurred_at=item.target.generated_at,
            )
        )
    return tuple(prepared)
