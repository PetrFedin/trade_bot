from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.trading import OrderIntent, Side


@dataclass(frozen=True)
class RiskLimits:
    maximum_order_notional: Decimal
    maximum_symbol_notional: Decimal
    maximum_gross_notional: Decimal
    maximum_price_age_seconds: Decimal = Decimal("15")
    maximum_spread_bps: Decimal = Decimal("50")
    maximum_slippage_bps: Decimal = Decimal("50")
    maximum_daily_loss: Decimal = Decimal("1000000")
    maximum_drawdown: Decimal = Decimal("1000000")
    maximum_turnover_notional: Decimal = Decimal("100000000")

    def validate(self) -> None:
        for name, value in (
            ("maximum_order_notional", self.maximum_order_notional),
            ("maximum_symbol_notional", self.maximum_symbol_notional),
            ("maximum_gross_notional", self.maximum_gross_notional),
            ("maximum_price_age_seconds", self.maximum_price_age_seconds),
            ("maximum_spread_bps", self.maximum_spread_bps),
            ("maximum_slippage_bps", self.maximum_slippage_bps),
            ("maximum_daily_loss", self.maximum_daily_loss),
            ("maximum_drawdown", self.maximum_drawdown),
            ("maximum_turnover_notional", self.maximum_turnover_notional),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")


@dataclass(frozen=True)
class RiskContext:
    price_timestamp: datetime
    decision_time: datetime
    market_open: bool = True
    halted: bool = False
    spread_bps: Decimal = Decimal("0")
    estimated_slippage_bps: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")
    drawdown: Decimal = Decimal("0")
    turnover_notional: Decimal = Decimal("0")

    def validate(self) -> None:
        for name, value in (("price_timestamp", self.price_timestamp), ("decision_time", self.decision_time)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.price_timestamp > self.decision_time:
            raise ValueError("price_timestamp cannot be in the future")
        for name, value in (
            ("spread_bps", self.spread_bps),
            ("estimated_slippage_bps", self.estimated_slippage_bps),
            ("drawdown", self.drawdown),
            ("turnover_notional", self.turnover_notional),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not self.daily_pnl.is_finite():
            raise ValueError("daily_pnl must be finite")


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
        context: RiskContext | None = None,
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

        if context is not None:
            context.validate()
            age_seconds = Decimal(str((context.decision_time - context.price_timestamp).total_seconds()))
            if age_seconds > self.limits.maximum_price_age_seconds:
                reasons.append("STALE_PRICE")
            if not context.market_open:
                reasons.append("MARKET_CLOSED")
            if context.halted:
                reasons.append("INSTRUMENT_HALTED")
            if context.spread_bps > self.limits.maximum_spread_bps:
                reasons.append("SPREAD_LIMIT_EXCEEDED")
            if context.estimated_slippage_bps > self.limits.maximum_slippage_bps:
                reasons.append("SLIPPAGE_LIMIT_EXCEEDED")
            if context.daily_pnl <= -self.limits.maximum_daily_loss:
                reasons.append("DAILY_LOSS_LIMIT_REACHED")
            if context.drawdown >= self.limits.maximum_drawdown:
                reasons.append("DRAWDOWN_LIMIT_REACHED")
            if context.turnover_notional + order_notional > self.limits.maximum_turnover_notional:
                reasons.append("TURNOVER_LIMIT_EXCEEDED")

        return RiskDecision(
            approved=not reasons,
            reasons=tuple(sorted(set(reasons))),
            order_notional=order_notional,
            projected_symbol_notional=projected_symbol,
            projected_gross_notional=projected_gross,
        )
