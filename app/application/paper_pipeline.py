from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from enum import StrEnum

from app.domain.trading import Bar, OrderIntent, Side, TargetPosition
from app.execution.execution_facts import ExecutionFactStore
from app.marketdata.validation import MarketDataPolicy, validate_bar_series
from app.portfolio.ledger import PortfolioLedger
from app.risk.evidence import RecordedRiskDecision, RiskAdmissionService
from app.risk.pretrade import PreTradeRiskEngine, RiskContext, RiskDecision
from app.strategy.momentum import LongOnlyMomentumStrategy


class PlanningMode(StrEnum):
    REPLAY = "REPLAY"
    OPERATIONAL = "OPERATIONAL"


class MarketDataNotReady(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__(f"MARKET_DATA_NOT_READY:{','.join(reasons)}")


class PaperTradingPipeline:
    """Deterministic trading slice with an explicit replay/operational clock boundary."""

    def __init__(
        self,
        *,
        strategy: LongOnlyMomentumStrategy,
        ledger: PortfolioLedger,
        risk: PreTradeRiskEngine,
        mode: PlanningMode,
        market_data_policy: MarketDataPolicy | None = None,
        risk_admission: RiskAdmissionService | None = None,
        execution_facts: ExecutionFactStore | None = None,
    ) -> None:
        if risk_admission is not None and risk_admission.engine is not risk:
            raise ValueError("risk_admission must use the pipeline risk engine")
        self.strategy = strategy
        self.ledger = ledger
        self.risk = risk
        self.mode = PlanningMode(mode)
        self.market_data_policy = MarketDataPolicy() if market_data_policy is None else market_data_policy
        self.market_data_policy.validate()
        self.risk_admission = risk_admission
        self.execution_facts = execution_facts
        self.last_recorded_risk: RecordedRiskDecision | None = None

    def plan(
        self,
        bars: Sequence[Bar],
        *,
        decision_time: datetime | None = None,
        kill_switch_engaged: bool = False,
        risk_context: RiskContext | None = None,
    ) -> tuple[TargetPosition, OrderIntent | None, RiskDecision | None]:
        if self.execution_facts is not None and self.execution_facts.unresolved_count() > 0:
            raise RuntimeError("EXECUTION_ACCOUNTING_NOT_CONVERGED")

        operational_clock = self._operational_market_data_gate(bars, decision_time=decision_time)
        target = self.strategy.target(bars)
        decision_clock = target.generated_at if operational_clock is None else operational_clock
        if decision_clock.tzinfo is None or decision_clock.utcoffset() is None:
            raise ValueError("decision_time must be timezone-aware")

        current = self.ledger.position(target.symbol)
        delta = target.quantity - current.quantity
        if delta == 0:
            self.last_recorded_risk = None
            return target, None, None
        side = Side.BUY if delta > 0 else Side.SELL
        quantity = abs(delta)
        raw_id = (
            f"{target.strategy_id}|{target.symbol}|{target.generated_at.isoformat()}|"
            f"{side.value}|{quantity}|{target.reference_price}"
        )
        intent = OrderIntent(
            intent_id=hashlib.sha256(raw_id.encode("utf-8")).hexdigest(),
            symbol=target.symbol,
            side=side,
            quantity=quantity,
            limit_price=target.reference_price,
            created_at=target.generated_at,
            strategy_id=target.strategy_id,
        )
        prices = {target.symbol: target.reference_price}
        current_symbol_notional = current.quantity * target.reference_price
        current_gross_notional = self.ledger.gross_notional(prices)
        effective_context = self._risk_context(
            target,
            risk_context,
            decision_time=decision_clock,
        )
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
                evaluated_at=decision_clock,
            )
            self.last_recorded_risk = recorded
            decision = recorded.decision
        return target, intent, decision

    def _operational_market_data_gate(
        self,
        bars: Sequence[Bar],
        *,
        decision_time: datetime | None,
    ) -> datetime | None:
        if self.mode is PlanningMode.REPLAY:
            if decision_time is not None and (
                decision_time.tzinfo is None or decision_time.utcoffset() is None
            ):
                raise ValueError("decision_time must be timezone-aware")
            return decision_time

        if decision_time is None:
            raise ValueError("OPERATIONAL_DECISION_TIME_REQUIRED")
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise ValueError("decision_time must be timezone-aware")
        quality = validate_bar_series(
            bars,
            now=decision_time,
            policy=self.market_data_policy,
        )
        if not quality.ready:
            self.last_recorded_risk = None
            raise MarketDataNotReady(quality.reasons)
        return decision_time

    def _risk_context(
        self,
        target: TargetPosition,
        supplied: RiskContext | None,
        *,
        decision_time: datetime,
    ) -> RiskContext:
        if supplied is None:
            return RiskContext(
                price_timestamp=target.generated_at,
                decision_time=decision_time,
                available_cash=self.ledger.cash,
            )
        if self.mode is PlanningMode.OPERATIONAL and supplied.decision_time != decision_time:
            raise ValueError("RISK_CONTEXT_DECISION_TIME_MISMATCH")
        if supplied.available_cash is None:
            return replace(supplied, available_cash=self.ledger.cash)
        if supplied.available_cash != self.ledger.cash:
            raise ValueError("risk_context available_cash disagrees with durable portfolio cash")
        return supplied
