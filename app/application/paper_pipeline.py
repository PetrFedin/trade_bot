from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal

from app.domain.trading import Bar, OrderIntent, Side, TargetPosition
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import PreTradeRiskEngine, RiskDecision
from app.strategy.momentum import LongOnlyMomentumStrategy


class PaperTradingPipeline:
    """Deterministic paper-only vertical slice through strategy, portfolio and risk."""

    def __init__(
        self,
        *,
        strategy: LongOnlyMomentumStrategy,
        ledger: PortfolioLedger,
        risk: PreTradeRiskEngine,
    ) -> None:
        self.strategy = strategy
        self.ledger = ledger
        self.risk = risk

    def plan(self, bars: Sequence[Bar], *, kill_switch_engaged: bool = False) -> tuple[TargetPosition, OrderIntent | None, RiskDecision | None]:
        target = self.strategy.target(bars)
        current = self.ledger.position(target.symbol)
        delta = target.quantity - current.quantity
        if delta == 0:
            return target, None, None
        side = Side.BUY if delta > 0 else Side.SELL
        quantity = abs(delta)
        raw_id = f"{target.strategy_id}|{target.symbol}|{target.generated_at.isoformat()}|{side.value}|{quantity}|{target.reference_price}"
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
        decision = self.risk.evaluate(
            intent,
            current_symbol_notional=current_symbol_notional,
            current_gross_notional=current_gross_notional,
            kill_switch_engaged=kill_switch_engaged,
        )
        return target, intent, decision
