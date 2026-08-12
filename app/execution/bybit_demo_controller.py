from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from app.execution.bybit_demo import (
    BybitDemoOrderRequest,
    BybitDemoProtectionRequest,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoTradePlan,
    execution_levels,
)
from app.strategy.crypto_session_risk import (
    CryptoSessionRiskPolicy,
    CryptoSessionRiskState,
    evaluate_crypto_session_risk,
)


@dataclass(frozen=True)
class BybitDemoEntryPlan:
    eligible: bool
    reasons: tuple[str, ...]
    order: BybitDemoOrderRequest | None
    protection_after_fill_required: bool
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoProtectionPlan:
    eligible: bool
    reasons: tuple[str, ...]
    protection: BybitDemoProtectionRequest | None
    live_mainnet_order_routing_allowed: bool = False


def plan_bybit_demo_entry(
    trade_plan: CryptoTradePlan,
    *,
    instrument: BybitInstrumentSpec,
    session_state: CryptoSessionRiskState,
    session_policy: CryptoSessionRiskPolicy | None = None,
) -> BybitDemoEntryPlan:
    """Turn an accepted research trade plan into a quantized demo-only order intent."""

    instrument.validate()
    if instrument.symbol != trade_plan.symbol:
        raise ValueError("Bybit demo instrument does not match crypto trade plan")
    risk = evaluate_crypto_session_risk(session_state, session_policy)
    if not risk.new_entries_allowed:
        return BybitDemoEntryPlan(
            eligible=False,
            reasons=risk.reasons,
            order=None,
            protection_after_fill_required=False,
        )
    quantity = instrument.normalize_market_quantity(
        trade_plan.reference_quantity,
        reference_price=trade_plan.reference_price,
    )
    if quantity is None:
        return BybitDemoEntryPlan(
            eligible=False,
            reasons=("BYBIT_INSTRUMENT_MINIMUMS_REJECT_PLAN",),
            order=None,
            protection_after_fill_required=False,
        )
    side = "Buy" if trade_plan.side is CryptoSide.LONG else "Sell"
    return BybitDemoEntryPlan(
        eligible=True,
        reasons=(),
        order=BybitDemoOrderRequest(
            symbol=trade_plan.symbol,
            side=side,
            quantity=quantity,
            order_link_id=_order_link_id(trade_plan, action="ENTRY"),
            reduce_only=False,
        ),
        protection_after_fill_required=True,
    )


def plan_bybit_demo_protection_after_fill(
    trade_plan: CryptoTradePlan,
    *,
    actual_average_entry_price: Decimal,
    instrument: BybitInstrumentSpec,
    strategy_config: CryptoPerpStrategyConfig,
) -> BybitDemoProtectionPlan:
    """Build exchange-native full-position TP/SL from the reconciled demo fill."""

    instrument.validate()
    if instrument.symbol != trade_plan.symbol:
        raise ValueError("Bybit demo instrument does not match crypto trade plan")
    if actual_average_entry_price <= 0:
        raise ValueError("actual Bybit demo average entry price must be positive")
    levels = execution_levels(
        trade_plan,
        entry_price=actual_average_entry_price,
        config=strategy_config,
    )
    side = trade_plan.side.value
    target = instrument.normalize_target_price(side, levels.target_price)
    stop = instrument.normalize_protective_stop_price(side, levels.stop_price)
    request = BybitDemoProtectionRequest(
        symbol=trade_plan.symbol,
        side="Buy" if trade_plan.side is CryptoSide.LONG else "Sell",
        average_entry_price=actual_average_entry_price,
        take_profit_price=target,
        stop_loss_price=stop,
    )
    try:
        request.validate()
    except ValueError:
        return BybitDemoProtectionPlan(
            eligible=False,
            reasons=("BYBIT_QUANTIZED_PROTECTION_INVALID",),
            protection=None,
        )
    return BybitDemoProtectionPlan(
        eligible=True,
        reasons=(),
        protection=request,
    )


def plan_bybit_demo_reduce_only_close(
    trade_plan: CryptoTradePlan,
    *,
    open_quantity: Decimal,
    instrument: BybitInstrumentSpec,
) -> BybitDemoOrderRequest:
    """Create an emergency/manual demo close that cannot increase exposure."""

    instrument.validate()
    if instrument.symbol != trade_plan.symbol:
        raise ValueError("Bybit demo instrument does not match crypto trade plan")
    if open_quantity <= 0:
        raise ValueError("Bybit demo close quantity must be positive")
    normalized = instrument.normalize_market_quantity(
        open_quantity,
        reference_price=trade_plan.reference_price,
    )
    if normalized is None:
        raise ValueError("Bybit demo close quantity is below instrument minimums")
    close_side = "Sell" if trade_plan.side is CryptoSide.LONG else "Buy"
    return BybitDemoOrderRequest(
        symbol=trade_plan.symbol,
        side=close_side,
        quantity=normalized,
        order_link_id=_order_link_id(trade_plan, action="CLOSE"),
        reduce_only=True,
    )


def _order_link_id(trade_plan: CryptoTradePlan, *, action: str) -> str:
    if action not in {"ENTRY", "CLOSE"}:
        raise ValueError("unsupported Bybit demo order action")
    payload = "|".join(
        (
            trade_plan.symbol,
            trade_plan.side.value,
            trade_plan.decision_time,
            action,
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16].upper()
    suffix = "E" if action == "ENTRY" else "C"
    return f"ASTRA-DEMO-{suffix}-{digest}"