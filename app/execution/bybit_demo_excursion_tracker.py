from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_trade_monitor import BybitDemoTradeMonitorResult
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan

_ZERO = Decimal("0")


@dataclass(frozen=True)
class BybitDemoTradeExcursionState:
    symbol: str
    side: CryptoSide
    entry_price: Decimal
    initial_quantity: Decimal
    stop_fraction: Decimal
    observation_count: int = 0
    latest_server_time_ms: int | None = None
    latest_mark_price: Decimal | None = None
    latest_gross_r: Decimal = _ZERO
    observed_peak_favorable_r: Decimal = _ZERO
    observed_trough_r: Decimal = _ZERO
    latest_giveback_from_peak_r: Decimal = _ZERO
    current_quantity: Decimal | None = None
    partial_close_seen: bool = False
    exchange_unrealised_pnl_usdt: Decimal | None = None
    projected_initial_quantity_gross_pnl_usdt: Decimal = _ZERO
    current_quantity_gross_pnl_usdt: Decimal = _ZERO
    diagnostics_only: bool = True
    exit_threshold_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoTradeExcursionFinal:
    symbol: str
    side: CryptoSide
    observation_count: int
    observed_peak_favorable_r: Decimal
    observed_max_adverse_r: Decimal
    realized_gross_exit_r: Decimal
    observed_peak_capture_fraction: Decimal | None
    giveback_from_observed_peak_to_exit_r: Decimal
    exit_exceeded_observed_peak: bool
    positive_observed_peak_nonpositive_exit: bool
    partial_close_seen: bool
    diagnostics_only: bool = True
    exit_threshold_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def start_bybit_demo_trade_excursion(
    trade_plan: CryptoTradePlan,
    *,
    position: BybitDemoPosition,
) -> BybitDemoTradeExcursionState:
    """Initialize demo excursion diagnostics from the reconciled actual fill state."""

    expected_side = _position_side(trade_plan.side)
    if position.symbol != trade_plan.symbol or position.side != expected_side:
        raise ValueError("demo excursion start position does not match trade plan")
    if position.average_price is None or position.average_price <= 0:
        raise ValueError("demo excursion start requires a positive actual average entry")
    if position.size <= 0:
        raise ValueError("demo excursion start requires positive open quantity")
    if trade_plan.stop_fraction <= 0:
        raise ValueError("demo excursion start requires positive stop fraction")
    return BybitDemoTradeExcursionState(
        symbol=trade_plan.symbol,
        side=trade_plan.side,
        entry_price=position.average_price,
        initial_quantity=position.size,
        stop_fraction=trade_plan.stop_fraction,
        current_quantity=position.size,
        exchange_unrealised_pnl_usdt=position.unrealised_pnl,
    )


def observe_bybit_demo_trade_excursion(
    state: BybitDemoTradeExcursionState,
    *,
    position: BybitDemoPosition,
    quote: BybitDemoMarketQuote,
) -> BybitDemoTradeExcursionState:
    """Update observed MFE/MAE/giveback from a fresh mark-price snapshot."""

    _validate_state(state)
    quote.validate()
    expected_side = _position_side(state.side)
    if position.symbol != state.symbol or position.side != expected_side:
        raise ValueError("demo excursion observation position does not match active trade")
    if quote.symbol != state.symbol:
        raise ValueError("demo excursion quote does not match active trade")
    if position.average_price is None or position.average_price != state.entry_price:
        raise ValueError("demo excursion actual average entry changed unexpectedly")
    if position.size < 0:
        raise ValueError("demo excursion current quantity cannot be negative")
    if position.size > state.initial_quantity:
        raise ValueError("demo excursion tracker rejected increased same-symbol exposure")
    if (
        state.latest_server_time_ms is not None
        and quote.server_time_ms < state.latest_server_time_ms
    ):
        raise ValueError("demo excursion quote time cannot move backwards")

    gross_r = _price_r(
        side=state.side,
        entry_price=state.entry_price,
        price=quote.mark_price,
        stop_fraction=state.stop_fraction,
    )
    peak = max(state.observed_peak_favorable_r, gross_r, _ZERO)
    trough = min(state.observed_trough_r, gross_r, _ZERO)
    giveback = max(peak - gross_r, _ZERO)
    initial_gross = _gross_pnl(
        side=state.side,
        entry_price=state.entry_price,
        price=quote.mark_price,
        quantity=state.initial_quantity,
    )
    current_gross = _gross_pnl(
        side=state.side,
        entry_price=state.entry_price,
        price=quote.mark_price,
        quantity=position.size,
    )
    return replace(
        state,
        observation_count=state.observation_count + 1,
        latest_server_time_ms=quote.server_time_ms,
        latest_mark_price=quote.mark_price,
        latest_gross_r=gross_r,
        observed_peak_favorable_r=peak,
        observed_trough_r=trough,
        latest_giveback_from_peak_r=giveback,
        current_quantity=position.size,
        partial_close_seen=(state.partial_close_seen or position.size < state.initial_quantity),
        exchange_unrealised_pnl_usdt=position.unrealised_pnl,
        projected_initial_quantity_gross_pnl_usdt=initial_gross,
        current_quantity_gross_pnl_usdt=current_gross,
    )


def finalize_bybit_demo_trade_excursion(
    state: BybitDemoTradeExcursionState,
    *,
    trade: BybitDemoTradeMonitorResult,
) -> BybitDemoTradeExcursionFinal:
    """Compare terminal gross exit quality with the best favorable excursion actually observed."""

    _validate_state(state)
    if not trade.terminal:
        raise ValueError("demo excursion finalization requires a terminal trade")
    if trade.symbol != state.symbol or trade.entry_side != _position_side(state.side):
        raise ValueError("demo excursion terminal trade does not match active state")
    if trade.average_entry_price is None or trade.average_entry_price != state.entry_price:
        raise ValueError("demo excursion terminal entry price does not reconcile")
    if trade.average_exit_price is None or trade.average_exit_price <= 0:
        raise ValueError("demo excursion terminal trade requires average exit price")

    exit_r = _price_r(
        side=state.side,
        entry_price=state.entry_price,
        price=trade.average_exit_price,
        stop_fraction=state.stop_fraction,
    )
    peak = state.observed_peak_favorable_r
    capture = None if peak <= 0 else exit_r / peak
    giveback = max(peak - exit_r, _ZERO)
    return BybitDemoTradeExcursionFinal(
        symbol=state.symbol,
        side=state.side,
        observation_count=state.observation_count,
        observed_peak_favorable_r=peak,
        observed_max_adverse_r=max(-state.observed_trough_r, _ZERO),
        realized_gross_exit_r=exit_r,
        observed_peak_capture_fraction=capture,
        giveback_from_observed_peak_to_exit_r=giveback,
        exit_exceeded_observed_peak=exit_r > peak,
        positive_observed_peak_nonpositive_exit=(peak > 0 and exit_r <= 0),
        partial_close_seen=state.partial_close_seen,
    )


def summarize_bybit_demo_excursion_quality(
    finals: Sequence[BybitDemoTradeExcursionFinal],
) -> dict[str, Any]:
    """Aggregate observed demo excursion quality without authorizing exit retuning."""

    if any(item.live_mainnet_order_routing_allowed for item in finals):
        raise ValueError("demo excursion quality rejected mainnet-capable diagnostics")
    observed = [item for item in finals if item.observation_count > 0]
    positive_peak = [item for item in observed if item.observed_peak_favorable_r > 0]
    capture_values = [
        item.observed_peak_capture_fraction
        for item in positive_peak
        if item.observed_peak_capture_fraction is not None
    ]
    return {
        "qualification": "BYBIT_DEMO_OBSERVED_EXCURSION_QUALITY",
        "trade_count": len(finals),
        "trades_with_excursion_observations": len(observed),
        "positive_observed_peak_trade_count": len(positive_peak),
        "positive_peak_nonpositive_exit_count": sum(
            item.positive_observed_peak_nonpositive_exit for item in positive_peak
        ),
        "partial_close_seen_count": sum(item.partial_close_seen for item in finals),
        "exit_exceeded_observed_peak_count": sum(
            item.exit_exceeded_observed_peak for item in finals
        ),
        "average_observed_peak_favorable_r": _average(
            [item.observed_peak_favorable_r for item in observed]
        ),
        "average_observed_max_adverse_r": _average(
            [item.observed_max_adverse_r for item in observed]
        ),
        "average_giveback_from_observed_peak_to_exit_r": _average(
            [item.giveback_from_observed_peak_to_exit_r for item in positive_peak]
        ),
        "average_observed_peak_capture_fraction": _average(capture_values),
        "observed_peak_is_sampling_lower_bound": True,
        "diagnostics_only": True,
        "exit_threshold_retuning_allowed": False,
        "strategy_promotion_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _validate_state(state: BybitDemoTradeExcursionState) -> None:
    if state.entry_price <= 0 or state.initial_quantity <= 0 or state.stop_fraction <= 0:
        raise ValueError("demo excursion state has invalid positive fields")
    if state.observation_count < 0:
        raise ValueError("demo excursion observation count cannot be negative")
    if state.current_quantity is not None and (
        state.current_quantity < 0 or state.current_quantity > state.initial_quantity
    ):
        raise ValueError("demo excursion state has invalid current quantity")


def _position_side(side: CryptoSide) -> str:
    return "Buy" if side is CryptoSide.LONG else "Sell"


def _price_r(
    *,
    side: CryptoSide,
    entry_price: Decimal,
    price: Decimal,
    stop_fraction: Decimal,
) -> Decimal:
    if price <= 0:
        raise ValueError("demo excursion price must be positive")
    move = (
        price / entry_price - Decimal("1")
        if side is CryptoSide.LONG
        else entry_price / price - Decimal("1")
    )
    return move / stop_fraction


def _gross_pnl(
    *,
    side: CryptoSide,
    entry_price: Decimal,
    price: Decimal,
    quantity: Decimal,
) -> Decimal:
    if quantity < 0:
        raise ValueError("demo excursion quantity cannot be negative")
    move = price - entry_price if side is CryptoSide.LONG else entry_price - price
    return move * quantity


def _average(values: Sequence[Decimal]) -> float | None:
    if not values:
        return None
    return float(sum(values, start=_ZERO) / Decimal(len(values)))
