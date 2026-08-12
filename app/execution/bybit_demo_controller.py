from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from app.execution.bybit_demo import (
    BybitDemoOrderRequest,
    BybitDemoProtectionRequest,
    BybitDemoRunnerProtectionRequest,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoTradePlan,
    execution_levels,
)
from app.strategy.crypto_profit_runner import (
    CryptoProfitRunnerPolicy,
    build_crypto_profit_runner_levels,
)
from app.strategy.crypto_session_risk import (
    CryptoSessionRiskPolicy,
    CryptoSessionRiskState,
    evaluate_crypto_session_risk,
)

_BPS = Decimal("10000")
_ONE = Decimal("1")
_POST_FILL_RISK_TOLERANCE = Decimal("1.05")


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


@dataclass(frozen=True)
class BybitDemoRunnerProtectionPlan:
    eligible: bool
    reasons: tuple[str, ...]
    protection: BybitDemoRunnerProtectionRequest | None
    flatten_required: bool
    modeled_stop_loss_after_cost_usdt: Decimal | None
    runner_activation_net_profit_usd: Decimal
    runner_protected_net_profit_usd: Decimal
    profit_cap_net_profit_usd: Decimal | None = None
    live_mainnet_order_routing_allowed: bool = False


def plan_bybit_demo_entry(
    trade_plan: CryptoTradePlan,
    *,
    instrument: BybitInstrumentSpec,
    session_state: CryptoSessionRiskState,
    session_policy: CryptoSessionRiskPolicy | None = None,
    runner_policy: CryptoProfitRunnerPolicy | None = None,
) -> BybitDemoEntryPlan:
    """Turn an accepted >=$20 research trade plan into a quantized demo-only order intent."""

    instrument.validate()
    active_runner = CryptoProfitRunnerPolicy() if runner_policy is None else runner_policy
    active_runner.validate()
    if instrument.symbol != trade_plan.symbol:
        raise ValueError("Bybit demo instrument does not match crypto trade plan")
    if trade_plan.target_net_profit_usd < active_runner.activation_net_profit_usd:
        return BybitDemoEntryPlan(
            eligible=False,
            reasons=("CRYPTO_ENTRY_MINIMUM_20_USD_NET_EDGE_REQUIRED",),
            order=None,
            protection_after_fill_required=False,
        )
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


def plan_bybit_demo_runner_protection_after_fill(
    trade_plan: CryptoTradePlan,
    *,
    actual_average_entry_price: Decimal,
    actual_filled_quantity: Decimal,
    instrument: BybitInstrumentSpec,
    strategy_config: CryptoPerpStrategyConfig,
    runner_policy: CryptoProfitRunnerPolicy | None = None,
) -> BybitDemoRunnerProtectionPlan:
    """Build hard SL + $20-activated trailing runner with no take-profit ceiling.

    The trailing distance is chosen so that its initial normal-fill protection corresponds to
    about $15 net after modeled fees/slippage. That amount is a protection objective, not a
    guarantee: gaps and execution slippage can realize less.
    """

    instrument.validate()
    active_runner = CryptoProfitRunnerPolicy() if runner_policy is None else runner_policy
    active_runner.validate()
    if instrument.symbol != trade_plan.symbol:
        raise ValueError("Bybit demo instrument does not match crypto trade plan")
    if actual_average_entry_price <= 0:
        raise ValueError("actual Bybit demo average entry price must be positive")
    if actual_filled_quantity <= 0:
        raise ValueError("actual Bybit demo filled quantity must be positive")
    if trade_plan.target_net_profit_usd < active_runner.activation_net_profit_usd:
        return BybitDemoRunnerProtectionPlan(
            eligible=False,
            reasons=("CRYPTO_ENTRY_MINIMUM_20_USD_NET_EDGE_REQUIRED",),
            protection=None,
            flatten_required=True,
            modeled_stop_loss_after_cost_usdt=None,
            runner_activation_net_profit_usd=active_runner.activation_net_profit_usd,
            runner_protected_net_profit_usd=active_runner.protected_net_profit_usd,
        )

    raw_runner = build_crypto_profit_runner_levels(
        trade_plan,
        actual_average_entry_price=actual_average_entry_price,
        actual_filled_quantity=actual_filled_quantity,
        strategy_config=strategy_config,
        policy=active_runner,
    )
    side = trade_plan.side.value
    activation = instrument.normalize_target_price(side, raw_runner.activation_price)
    protected = instrument.normalize_target_price(
        side,
        raw_runner.protected_price_at_activation,
    )
    trailing_distance = abs(activation - protected)

    raw_hard_stop = _hard_stop_price(
        trade_plan,
        actual_average_entry_price=actual_average_entry_price,
    )
    hard_stop = instrument.normalize_protective_stop_price(side, raw_hard_stop)
    request = BybitDemoRunnerProtectionRequest(
        symbol=trade_plan.symbol,
        side="Buy" if trade_plan.side is CryptoSide.LONG else "Sell",
        average_entry_price=actual_average_entry_price,
        stop_loss_price=hard_stop,
        trailing_stop_distance=trailing_distance,
        trailing_active_price=activation,
    )
    try:
        request.validate()
    except ValueError:
        return BybitDemoRunnerProtectionPlan(
            eligible=False,
            reasons=("BYBIT_QUANTIZED_RUNNER_PROTECTION_INVALID",),
            protection=None,
            flatten_required=True,
            modeled_stop_loss_after_cost_usdt=None,
            runner_activation_net_profit_usd=active_runner.activation_net_profit_usd,
            runner_protected_net_profit_usd=active_runner.protected_net_profit_usd,
        )

    modeled_stop_loss = _modeled_stop_loss_after_cost(
        side=trade_plan.side,
        actual_average_entry_price=actual_average_entry_price,
        actual_filled_quantity=actual_filled_quantity,
        raw_stop_price=hard_stop,
        strategy_config=strategy_config,
    )
    risk_limit = trade_plan.risk_budget_usdt * _POST_FILL_RISK_TOLERANCE
    risk_breached = modeled_stop_loss > risk_limit
    reasons = ("POST_FILL_RISK_BUDGET_EXCEEDED",) if risk_breached else ()
    return BybitDemoRunnerProtectionPlan(
        eligible=not risk_breached,
        reasons=reasons,
        protection=request,
        flatten_required=risk_breached,
        modeled_stop_loss_after_cost_usdt=modeled_stop_loss,
        runner_activation_net_profit_usd=active_runner.activation_net_profit_usd,
        runner_protected_net_profit_usd=active_runner.protected_net_profit_usd,
        profit_cap_net_profit_usd=None,
    )


def plan_bybit_demo_protection_after_fill(
    trade_plan: CryptoTradePlan,
    *,
    actual_average_entry_price: Decimal,
    instrument: BybitInstrumentSpec,
    strategy_config: CryptoPerpStrategyConfig,
) -> BybitDemoProtectionPlan:
    """Legacy fixed TP/SL planner retained for fixed-target replay/demo comparisons."""

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


def _hard_stop_price(
    trade_plan: CryptoTradePlan,
    *,
    actual_average_entry_price: Decimal,
) -> Decimal:
    move = actual_average_entry_price * trade_plan.stop_fraction
    if trade_plan.side is CryptoSide.LONG:
        return actual_average_entry_price - move
    return actual_average_entry_price + move


def _modeled_stop_loss_after_cost(
    *,
    side: CryptoSide,
    actual_average_entry_price: Decimal,
    actual_filled_quantity: Decimal,
    raw_stop_price: Decimal,
    strategy_config: CryptoPerpStrategyConfig,
) -> Decimal:
    fee = strategy_config.taker_fee_rate
    slippage = strategy_config.slippage_bps_per_fill / _BPS
    entry_fee = actual_average_entry_price * actual_filled_quantity * fee
    if side is CryptoSide.LONG:
        exit_execution = raw_stop_price * (_ONE - slippage)
        gross_loss = (actual_average_entry_price - exit_execution) * actual_filled_quantity
    else:
        exit_execution = raw_stop_price * (_ONE + slippage)
        gross_loss = (exit_execution - actual_average_entry_price) * actual_filled_quantity
    exit_fee = exit_execution * actual_filled_quantity * fee
    return gross_loss + entry_fee + exit_fee


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
