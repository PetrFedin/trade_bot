from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class OperationalSloPolicy:
    maximum_market_data_age_seconds: Decimal = Decimal("15")
    maximum_stream_silence_seconds: Decimal = Decimal("45")
    maximum_broker_latency_ms: Decimal = Decimal("2000")
    maximum_broker_error_fraction: Decimal = Decimal("0.05")
    maximum_uncertain_orders: int = 0
    maximum_reconciliation_age_seconds: Decimal = Decimal("60")
    maximum_cash_mismatch: Decimal = Decimal("0.01")
    maximum_position_mismatches: int = 0
    maximum_daily_loss: Decimal = Decimal("1000")
    maximum_drawdown: Decimal = Decimal("1500")

    def validate(self) -> None:
        for name, value in (
            ("maximum_market_data_age_seconds", self.maximum_market_data_age_seconds),
            ("maximum_stream_silence_seconds", self.maximum_stream_silence_seconds),
            ("maximum_broker_latency_ms", self.maximum_broker_latency_ms),
            ("maximum_reconciliation_age_seconds", self.maximum_reconciliation_age_seconds),
            ("maximum_cash_mismatch", self.maximum_cash_mismatch),
            ("maximum_daily_loss", self.maximum_daily_loss),
            ("maximum_drawdown", self.maximum_drawdown),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if (
            not self.maximum_broker_error_fraction.is_finite()
            or self.maximum_broker_error_fraction < 0
            or self.maximum_broker_error_fraction > 1
        ):
            raise ValueError("maximum_broker_error_fraction must be within [0, 1]")
        if self.maximum_uncertain_orders < 0 or self.maximum_position_mismatches < 0:
            raise ValueError("count thresholds must be non-negative")


@dataclass(frozen=True)
class OperationalSnapshot:
    market_data_age_seconds: Decimal
    stream_silence_seconds: Decimal
    broker_latency_ms: Decimal
    broker_error_fraction: Decimal
    uncertain_orders: int
    reconciliation_age_seconds: Decimal
    cash_mismatch: Decimal
    position_mismatches: int
    daily_pnl: Decimal
    drawdown: Decimal
    kill_switch_engaged: bool
    market_data_ready: bool
    stream_ready: bool
    broker_connected: bool
    portfolio_reconciled: bool
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    def validate(self) -> None:
        for name, value in (
            ("market_data_age_seconds", self.market_data_age_seconds),
            ("stream_silence_seconds", self.stream_silence_seconds),
            ("broker_latency_ms", self.broker_latency_ms),
            ("broker_error_fraction", self.broker_error_fraction),
            ("reconciliation_age_seconds", self.reconciliation_age_seconds),
            ("cash_mismatch", self.cash_mismatch),
            ("drawdown", self.drawdown),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not self.daily_pnl.is_finite():
            raise ValueError("daily_pnl must be finite")
        if self.broker_error_fraction > 1:
            raise ValueError("broker_error_fraction must be within [0, 1]")
        if self.uncertain_orders < 0 or self.position_mismatches < 0:
            raise ValueError("counts must be non-negative")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise ValueError("operational qualification cannot enable external/live routing")


@dataclass(frozen=True)
class OperationalReadiness:
    ready_for_paper_operation: bool
    reasons: tuple[str, ...]
    degraded: bool
    live_trading_allowed: bool = False


class OperationalReadinessEvaluator:
    """Fail-closed health/SLO gate for the stable paper-trading product."""

    def __init__(self, policy: OperationalSloPolicy | None = None) -> None:
        self.policy = OperationalSloPolicy() if policy is None else policy
        self.policy.validate()

    def evaluate(self, snapshot: OperationalSnapshot) -> OperationalReadiness:
        snapshot.validate()
        reasons: set[str] = set()
        if not snapshot.market_data_ready:
            reasons.add("MARKET_DATA_NOT_READY")
        if snapshot.market_data_age_seconds > self.policy.maximum_market_data_age_seconds:
            reasons.add("MARKET_DATA_STALE")
        if not snapshot.stream_ready:
            reasons.add("TRADE_STREAM_NOT_READY")
        if snapshot.stream_silence_seconds > self.policy.maximum_stream_silence_seconds:
            reasons.add("TRADE_STREAM_SILENT")
        if not snapshot.broker_connected:
            reasons.add("BROKER_DISCONNECTED")
        if snapshot.broker_latency_ms > self.policy.maximum_broker_latency_ms:
            reasons.add("BROKER_LATENCY_SLO_BREACH")
        if snapshot.broker_error_fraction > self.policy.maximum_broker_error_fraction:
            reasons.add("BROKER_ERROR_SLO_BREACH")
        if snapshot.uncertain_orders > self.policy.maximum_uncertain_orders:
            reasons.add("UNCERTAIN_ORDERS_PRESENT")
        if snapshot.reconciliation_age_seconds > self.policy.maximum_reconciliation_age_seconds:
            reasons.add("RECONCILIATION_STALE")
        if not snapshot.portfolio_reconciled:
            reasons.add("PORTFOLIO_NOT_RECONCILED")
        if snapshot.cash_mismatch > self.policy.maximum_cash_mismatch:
            reasons.add("CASH_MISMATCH")
        if snapshot.position_mismatches > self.policy.maximum_position_mismatches:
            reasons.add("POSITION_MISMATCH")
        if snapshot.daily_pnl <= -self.policy.maximum_daily_loss:
            reasons.add("DAILY_LOSS_SLO_BREACH")
        if snapshot.drawdown >= self.policy.maximum_drawdown:
            reasons.add("DRAWDOWN_SLO_BREACH")
        if snapshot.kill_switch_engaged:
            reasons.add("KILL_SWITCH_ENGAGED")
        return OperationalReadiness(
            ready_for_paper_operation=not reasons,
            reasons=tuple(sorted(reasons)),
            degraded=bool(reasons),
            live_trading_allowed=False,
        )
