from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.trading import OrderIntent, Side


@dataclass(frozen=True)
class RiskLimits:
    maximum_order_notional: Decimal
    maximum_symbol_notional: Decimal
    maximum_gross_notional: Decimal

    def validate(self) -> None:
        for name, value in (
            ("maximum_order_notional", self.maximum_order_notional),
            ("maximum_symbol_notional", self.maximum_symbol_notional),
            ("maximum_gross_notional", self.maximum_gross_notional),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...]
    order_notional: Decimal
    projected_symbol_notional: Decimal
    projected_gross_notional: Decimal


class PreTradeRiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        limits.validate()
        self.limits = limits

    def evaluate(
        self,
        intent: OrderIntent,
        *,
        current_symbol_notional: Decimal,
        current_gross_notional: Decimal,
        kill_switch_engaged: bool = False,
    ) -> RiskDecision:
        intent.validate()
        for name, value in (
            ("current_symbol_notional", current_symbol_notional),
            ("current_gross_notional", current_gross_notional),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        order_notional = intent.quantity * intent.limit_price
        if intent.side is Side.BUY:
            projected_symbol = current_symbol_notional + order_notional
            projected_gross = current_gross_notional + order_notional
        else:
            if order_notional > current_symbol_notional:
                return RiskDecision(
                    approved=False,
                    reasons=("SELL_EXCEEDS_CURRENT_LONG_EXPOSURE",),
                    order_notional=order_notional,
                    projected_symbol_notional=current_symbol_notional,
                    projected_gross_notional=current_gross_notional,
                )
            projected_symbol = current_symbol_notional - order_notional
            projected_gross = max(Decimal("0"), current_gross_notional - order_notional)
        reasons: list[str] = []
        if kill_switch_engaged:
            reasons.append("KILL_SWITCH_ENGAGED")
        if order_notional > self.limits.maximum_order_notional:
            reasons.append("ORDER_NOTIONAL_LIMIT_EXCEEDED")
        if projected_symbol > self.limits.maximum_symbol_notional:
            reasons.append("SYMBOL_NOTIONAL_LIMIT_EXCEEDED")
        if projected_gross > self.limits.maximum_gross_notional:
            reasons.append("GROSS_NOTIONAL_LIMIT_EXCEEDED")
        return RiskDecision(
            approved=not reasons,
            reasons=tuple(reasons),
            order_notional=order_notional,
            projected_symbol_notional=projected_symbol,
            projected_gross_notional=projected_gross,
        )
