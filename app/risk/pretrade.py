from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from app.domain.trading import OrderIntent, Side


def _validate_mark_prices(prices: Mapping[str, Decimal]) -> None:
    for symbol, price in prices.items():
        normalized = symbol.strip().upper()
        if not normalized or normalized != symbol:
            raise ValueError("portfolio mark symbols must be normalized uppercase")
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise ValueError(f"portfolio mark price must be positive and finite: {symbol}")


class RiskEvaluationMode(StrEnum):
    """Explicit trust boundary for pre-trade risk inputs.

    REPLAY is intentionally permissive for deterministic historical/research
    evaluation. OPERATIONAL is fail-closed and requires a complete measured
    ``OperationalRiskContext``. There is deliberately no implicit/default mode.
    """

    REPLAY = "REPLAY"
    OPERATIONAL = "OPERATIONAL"


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
    maximum_liquidity_participation_fraction: Decimal = Decimal("0.10")
    maximum_position_fraction_of_equity: Decimal = Decimal("0.20")
    maximum_sector_fraction_of_equity: Decimal = Decimal("0.40")
    maximum_annualized_volatility: Decimal = Decimal("2")

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
            ("maximum_annualized_volatility", self.maximum_annualized_volatility),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name, value in (
            (
                "maximum_liquidity_participation_fraction",
                self.maximum_liquidity_participation_fraction,
            ),
            ("maximum_position_fraction_of_equity", self.maximum_position_fraction_of_equity),
            ("maximum_sector_fraction_of_equity", self.maximum_sector_fraction_of_equity),
        ):
            if not value.is_finite() or value <= 0 or value > 1:
                raise ValueError(f"{name} must be within (0, 1]")


@dataclass(frozen=True)
class RiskContext:
    """Replay/research risk observations.

    Several observations retain deterministic defaults for explicit REPLAY use.
    Production admission must never rely on this type; OPERATIONAL evaluation
    accepts only ``OperationalRiskContext``.
    """

    price_timestamp: datetime
    decision_time: datetime
    market_open: bool = True
    halted: bool = False
    spread_bps: Decimal = Decimal("0")
    estimated_slippage_bps: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")
    drawdown: Decimal = Decimal("0")
    turnover_notional: Decimal = Decimal("0")
    average_daily_dollar_volume: Decimal | None = None
    portfolio_equity: Decimal | None = None
    sector_notional: Decimal | None = None
    annualized_volatility: Decimal | None = None
    available_cash: Decimal | None = None
    portfolio_mark_prices: Mapping[str, Decimal] | None = None

    def validate(self) -> None:
        for name, value in (
            ("price_timestamp", self.price_timestamp),
            ("decision_time", self.decision_time),
        ):
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
        for name, value in (
            ("average_daily_dollar_volume", self.average_daily_dollar_volume),
            ("portfolio_equity", self.portfolio_equity),
        ):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError(f"{name} must be positive and finite when supplied")
        for name, value in (
            ("sector_notional", self.sector_notional),
            ("annualized_volatility", self.annualized_volatility),
            ("available_cash", self.available_cash),
        ):
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError(f"{name} must be finite and non-negative when supplied")
        if self.portfolio_mark_prices is not None:
            _validate_mark_prices(self.portfolio_mark_prices)


@dataclass(frozen=True)
class OperationalRiskContext:
    """Complete measured risk observations required for operational admission.

    Unlike ``RiskContext``, this type has no optimistic/defaulted observations.
    It also carries the complete factual mark-price snapshot used to value every
    open position plus the current target symbol before a risk decision is made.
    """

    price_timestamp: datetime
    decision_time: datetime
    market_open: bool
    halted: bool
    spread_bps: Decimal
    estimated_slippage_bps: Decimal
    daily_pnl: Decimal
    drawdown: Decimal
    turnover_notional: Decimal
    average_daily_dollar_volume: Decimal
    portfolio_equity: Decimal
    sector_notional: Decimal
    annualized_volatility: Decimal
    available_cash: Decimal
    portfolio_mark_prices: Mapping[str, Decimal]

    def to_risk_context(self) -> RiskContext:
        context = RiskContext(
            price_timestamp=self.price_timestamp,
            decision_time=self.decision_time,
            market_open=self.market_open,
            halted=self.halted,
            spread_bps=self.spread_bps,
            estimated_slippage_bps=self.estimated_slippage_bps,
            daily_pnl=self.daily_pnl,
            drawdown=self.drawdown,
            turnover_notional=self.turnover_notional,
            average_daily_dollar_volume=self.average_daily_dollar_volume,
            portfolio_equity=self.portfolio_equity,
            sector_notional=self.sector_notional,
            annualized_volatility=self.annualized_volatility,
            available_cash=self.available_cash,
            portfolio_mark_prices=dict(self.portfolio_mark_prices),
        )
        context.validate()
        return context

    def validate(self) -> None:
        if not isinstance(self.market_open, bool):
            raise ValueError("market_open must be boolean")
        if not isinstance(self.halted, bool):
            raise ValueError("halted must be boolean")
        _validate_mark_prices(self.portfolio_mark_prices)
        self.to_risk_context()


def risk_intent_fingerprint(intent: OrderIntent) -> str:
    """Stable identity/economics binding for a pre-trade risk decision."""

    intent.validate()
    material = json.dumps(
        {
            "intent_id": intent.intent_id,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "quantity": str(intent.quantity),
            "limit_price": str(intent.limit_price),
            "created_at": intent.created_at.astimezone(UTC).isoformat(),
            "strategy_id": intent.strategy_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...]
    order_notional: Decimal
    projected_symbol_notional: Decimal
    projected_gross_notional: Decimal
    intent_id: str = ""
    intent_fingerprint: str = ""


class PreTradeRiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        limits.validate()
        self.limits = limits

    @staticmethod
    def _context_for_mode(
        mode: RiskEvaluationMode,
        context: RiskContext | OperationalRiskContext | None,
    ) -> RiskContext | None:
        evaluation_mode = RiskEvaluationMode(mode)
        if evaluation_mode is RiskEvaluationMode.OPERATIONAL:
            if not isinstance(context, OperationalRiskContext):
                raise ValueError("OPERATIONAL_RISK_CONTEXT_REQUIRED")
            context.validate()
            return context.to_risk_context()
        if context is None:
            return None
        if isinstance(context, OperationalRiskContext):
            context.validate()
            return context.to_risk_context()
        context.validate()
        return context

    def evaluate(
        self,
        intent: OrderIntent,
        *,
        mode: RiskEvaluationMode,
        current_symbol_notional: Decimal,
        current_gross_notional: Decimal,
        kill_switch_engaged: bool = False,
        context: RiskContext | OperationalRiskContext | None = None,
    ) -> RiskDecision:
        intent.validate()
        evaluation_mode = RiskEvaluationMode(mode)
        effective_context = self._context_for_mode(evaluation_mode, context)
        intent_fingerprint = risk_intent_fingerprint(intent)
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
                    intent_id=intent.intent_id,
                    intent_fingerprint=intent_fingerprint,
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

        if effective_context is not None:
            age_seconds = Decimal(
                str(
                    (
                        effective_context.decision_time - effective_context.price_timestamp
                    ).total_seconds()
                )
            )
            if age_seconds > self.limits.maximum_price_age_seconds:
                reasons.append("STALE_PRICE")
            if not effective_context.market_open:
                reasons.append("MARKET_CLOSED")
            if effective_context.halted:
                reasons.append("INSTRUMENT_HALTED")
            if effective_context.spread_bps > self.limits.maximum_spread_bps:
                reasons.append("SPREAD_LIMIT_EXCEEDED")
            if effective_context.estimated_slippage_bps > self.limits.maximum_slippage_bps:
                reasons.append("SLIPPAGE_LIMIT_EXCEEDED")
            if effective_context.daily_pnl <= -self.limits.maximum_daily_loss:
                reasons.append("DAILY_LOSS_LIMIT_REACHED")
            if effective_context.drawdown >= self.limits.maximum_drawdown:
                reasons.append("DRAWDOWN_LIMIT_REACHED")
            if (
                effective_context.turnover_notional + order_notional
                > self.limits.maximum_turnover_notional
            ):
                reasons.append("TURNOVER_LIMIT_EXCEEDED")
            if (
                intent.side is Side.BUY
                and effective_context.available_cash is not None
                and order_notional > effective_context.available_cash
            ):
                reasons.append("INSUFFICIENT_AVAILABLE_CASH")
            if effective_context.average_daily_dollar_volume is not None:
                participation = order_notional / effective_context.average_daily_dollar_volume
                if participation > self.limits.maximum_liquidity_participation_fraction:
                    reasons.append("LIQUIDITY_PARTICIPATION_EXCEEDED")
            if effective_context.portfolio_equity is not None:
                if (
                    projected_symbol / effective_context.portfolio_equity
                    > self.limits.maximum_position_fraction_of_equity
                ):
                    reasons.append("POSITION_CONCENTRATION_EXCEEDED")
                if effective_context.sector_notional is not None:
                    projected_sector = (
                        effective_context.sector_notional + order_notional
                        if intent.side is Side.BUY
                        else max(
                            Decimal("0"),
                            effective_context.sector_notional - order_notional,
                        )
                    )
                    if (
                        projected_sector / effective_context.portfolio_equity
                        > self.limits.maximum_sector_fraction_of_equity
                    ):
                        reasons.append("SECTOR_CONCENTRATION_EXCEEDED")
            if (
                effective_context.annualized_volatility is not None
                and effective_context.annualized_volatility
                > self.limits.maximum_annualized_volatility
            ):
                reasons.append("VOLATILITY_LIMIT_EXCEEDED")

        return RiskDecision(
            approved=not reasons,
            reasons=tuple(sorted(set(reasons))),
            order_notional=order_notional,
            projected_symbol_notional=projected_symbol,
            projected_gross_notional=projected_gross,
            intent_id=intent.intent_id,
            intent_fingerprint=intent_fingerprint,
        )
