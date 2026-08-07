from __future__ import annotations

import hashlib
from collections.abc import Sequence

from app.domain.trading import Bar, OrderIntent, Side, TargetPosition
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
        if self.risk_admission is None:
            self.last_recorded_risk = None
            decision = self.risk.evaluate(
                intent,
                current_symbol_notional=current_symbol_notional,
                current_gross_notional=current_gross_notional,
                kill_switch_engaged=kill_switch_engaged,
                context=risk_context,
            )
        else:
            recorded = self.risk_admission.evaluate_and_record(
                intent,
                current_symbol_notional=current_symbol_notional,
                current_gross_notional=current_gross_notional,
                kill_switch_engaged=kill_switch_engaged,
                context=risk_context,
                evaluated_at=target.generated_at,
            )
            self.last_recorded_risk = recorded
            decision = recorded.decision
        return target, intent, decision
