from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from app.application.order_intents import order_intent_for_target
from app.domain.trading import Bar, OrderIntent, TargetPosition
from app.portfolio.ledger import PortfolioLedger
from app.risk.evidence import RecordedRiskDecision, RiskAdmissionService
from app.risk.pretrade import PreTradeRiskEngine, RiskContext, RiskDecision
from app.strategy.momentum import LongOnlyMomentumStrategy


class PaperTradingPipeline:
    """Deterministic paper-only vertical slice through strategy, portfolio and risk."""

    def __init__(
        self,
        *,
        strategy: LongOnlyMomentumStrategy,
        ledger: PortfolioLedger,
        risk: PreTradeRiskEngine,
        risk_admission: RiskAdmissionService | None = None,
    ) -> None:
        if risk_admission is not None and risk_admission.engine is not risk:
            raise ValueError("risk_admission must use the pipeline risk engine")
        self.strategy = strategy
        self.ledger = ledger
        self.risk = risk
        self.risk_admission = risk_admission
        self.last_recorded_risk: RecordedRiskDecision | None = None

    def plan(
        self,
        bars: Sequence[Bar],
        *,
        kill_switch_engaged: bool = False,
        risk_context: RiskContext | None = None,
    ) -> tuple[TargetPosition, OrderIntent | None, RiskDecision | None]:
        target = self.strategy.target(bars)
        current = self.ledger.position(target.symbol)
        intent = order_intent_for_target(
            target,
            current_quantity=current.quantity,
        )
        if intent is None:
            self.last_recorded_risk = None
            return target, None, None
        prices = {target.symbol: target.reference_price}
        current_symbol_notional = current.quantity * target.reference_price
        current_gross_notional = self.ledger.gross_notional(prices)
        effective_context = self._risk_context(target, risk_context)
        if self.risk_admission is None:
            self.last_recorded_risk = None
            decision = self.risk.evaluate(
                intent,
                current_symbol_notional=current_symbol_notional,
                current_gross_notional=current_gross_notional,
                kill_switch_engaged=kill_switch_engaged,
                context=effective_context,
            )
        else:
            recorded = self.risk_admission.evaluate_and_record(
                intent,
                current_symbol_notional=current_symbol_notional,
                current_gross_notional=current_gross_notional,
                kill_switch_engaged=kill_switch_engaged,
                context=effective_context,
                evaluated_at=target.generated_at,
            )
            self.last_recorded_risk = recorded
            decision = recorded.decision
        return target, intent, decision

    def _risk_context(
        self,
        target: TargetPosition,
        supplied: RiskContext | None,
    ) -> RiskContext:
        if supplied is None:
            return RiskContext(
                price_timestamp=target.generated_at,
                decision_time=target.generated_at,
                available_cash=self.ledger.cash,
            )
        if supplied.available_cash is None:
            return replace(supplied, available_cash=self.ledger.cash)
        if supplied.available_cash != self.ledger.cash:
            raise ValueError("risk_context available_cash disagrees with durable portfolio cash")
        return supplied
