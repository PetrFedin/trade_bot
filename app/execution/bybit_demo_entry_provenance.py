from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.execution.bybit_demo_cycle import BybitDemoCycleStatus
from app.execution.bybit_demo_ranked_fallback import (
    BybitDemoCandidateFallbackAttempt,
    BybitDemoResilientAccountSizedCycleResult,
)
from app.execution.bybit_demo_strategy_selector import BybitDemoStrategyCycleStatus
from app.strategy.crypto_perp import CryptoSide

_BPS = Decimal("10000")


@dataclass(frozen=True)
class BybitDemoEntryDecisionProvenance:
    entry_order_link_id: str
    symbol: str
    side: CryptoSide
    decision_time: str
    selected_signal_rank: int
    executable_candidate_count: int
    candidate_audit_count: int
    economic_shadow_selected_symbol: str | None
    economic_shadow_selected_side: str | None
    economic_shadow_differs_from_current: bool
    selected_after_fallback: bool
    fallback_attempts: tuple[BybitDemoCandidateFallbackAttempt, ...]
    expected_net_edge_usd: Decimal
    risk_budget_usdt: Decimal
    quality_score: Decimal
    target_net_profit_usd: Decimal
    planned_reference_price: Decimal
    planned_reference_quantity: Decimal
    planned_notional_usdt: Decimal
    modeled_round_trip_cost_usdt: Decimal
    pre_entry_quote_price: Decimal | None
    pre_entry_modeled_entry_price: Decimal | None
    pre_entry_original_quantity: Decimal | None
    pre_entry_adjusted_quantity: Decimal | None
    pre_entry_quote_resized: bool
    pre_entry_quantity_retention_fraction: Decimal | None
    actual_average_entry_price: Decimal
    actual_filled_quantity: Decimal
    actual_fill_notional_usdt: Decimal
    actual_fill_adverse_slippage_bps_vs_modeled_entry: Decimal | None
    account_taker_fee_rate: Decimal
    exit_mode: str
    runner_admission_reasons: tuple[str, ...]
    liquidation_safety_reason: str | None
    stop_to_liquidation_r: Decimal | None
    effective_account_equity_usdt: Decimal
    effective_peak_equity_usdt: Decimal
    margin_mode: str | None
    realized_pnl_used_for_selection: bool = False
    diagnostics_only: bool = True
    automatic_selector_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def build_bybit_demo_entry_decision_provenance(
    result: BybitDemoResilientAccountSizedCycleResult,
) -> BybitDemoEntryDecisionProvenance | None:
    """Capture why a protected demo entry was selected and how it actually filled.

    The record is deliberately outcome-free. It contains only information available before or at
    the protected entry boundary plus exchange/account facts observed while protecting that entry.
    Realized PnL, future MFE/MAE and terminal outcomes are excluded so this diagnostics payload can
    later be joined to final evidence without leaking future returns back into the selector.
    """

    if result.live_mainnet_order_routing_allowed:
        raise ValueError("entry provenance rejected a mainnet-capable resilient result")
    account = result.account_sized_result
    if account.live_mainnet_order_routing_allowed:
        raise ValueError("entry provenance rejected a mainnet-capable account result")
    strategy = account.strategy_cycle_result
    if strategy is None:
        return None
    if strategy.live_mainnet_order_routing_allowed:
        raise ValueError("entry provenance rejected a mainnet-capable strategy result")
    if strategy.status is not BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED:
        return None
    plan = strategy.selection.selected_trade_plan
    rank = strategy.selection.selected_signal_rank
    orchestrator = strategy.orchestrator_result
    if plan is None or rank is None or orchestrator is None:
        return None
    if orchestrator.live_mainnet_order_routing_allowed:
        raise ValueError("entry provenance rejected a mainnet-capable orchestrator result")
    cycle = orchestrator.cycle_result
    if cycle is None or cycle.status is not BybitDemoCycleStatus.PROTECTED:
        return None
    if cycle.live_mainnet_order_routing_allowed:
        raise ValueError("entry provenance rejected a mainnet-capable protected cycle")
    if cycle.entry_ack is None or not cycle.entry_ack.accepted or cycle.entry_ack.live_mainnet_order:
        raise ValueError("protected demo entry provenance requires a safe accepted entry ACK")
    position = cycle.reconciled_position
    if position is None or position.average_price is None or position.size <= 0:
        raise ValueError("protected demo entry provenance requires reconciled fill state")
    if cycle.account_taker_fee_rate is None or cycle.account_taker_fee_rate < 0:
        raise ValueError("protected demo entry provenance requires account taker fee")
    if cycle.exit_mode not in {"FIXED_20_TARGET", "OPEN_ENDED_RUNNER"}:
        raise ValueError("protected demo entry provenance requires resolved exit mode")

    original_quantity = strategy.pre_entry_original_quantity
    adjusted_quantity = strategy.pre_entry_adjusted_quantity
    retention = None
    if original_quantity is not None and adjusted_quantity is not None:
        if original_quantity <= 0 or adjusted_quantity <= 0:
            raise ValueError("entry provenance quote quantities must be positive")
        if adjusted_quantity > original_quantity:
            raise ValueError("entry provenance pre-entry resize cannot increase quantity")
        retention = adjusted_quantity / original_quantity

    modeled_entry = strategy.pre_entry_modeled_entry_price
    adverse_slippage_bps = None
    if modeled_entry is not None:
        if modeled_entry <= 0:
            raise ValueError("entry provenance modeled entry price must be positive")
        if plan.side is CryptoSide.LONG:
            adverse_slippage_bps = (
                (position.average_price - modeled_entry) / modeled_entry * _BPS
            )
        else:
            adverse_slippage_bps = (
                (modeled_entry - position.average_price) / modeled_entry * _BPS
            )

    selection = strategy.selection
    return BybitDemoEntryDecisionProvenance(
        entry_order_link_id=cycle.entry_ack.order_link_id,
        symbol=plan.symbol,
        side=plan.side,
        decision_time=plan.decision_time,
        selected_signal_rank=rank,
        executable_candidate_count=selection.executable_candidate_count,
        candidate_audit_count=len(selection.candidate_audit),
        economic_shadow_selected_symbol=selection.economic_shadow_selected_symbol,
        economic_shadow_selected_side=selection.economic_shadow_selected_side,
        economic_shadow_differs_from_current=selection.economic_shadow_differs_from_current,
        selected_after_fallback=result.selected_after_fallback,
        fallback_attempts=result.fallback_attempts,
        expected_net_edge_usd=plan.expected_net_edge_usd,
        risk_budget_usdt=plan.risk_budget_usdt,
        quality_score=plan.quality_score,
        target_net_profit_usd=plan.target_net_profit_usd,
        planned_reference_price=plan.reference_price,
        planned_reference_quantity=plan.reference_quantity,
        planned_notional_usdt=plan.notional_usdt,
        modeled_round_trip_cost_usdt=plan.estimated_round_trip_cost_usdt,
        pre_entry_quote_price=strategy.pre_entry_quote_price,
        pre_entry_modeled_entry_price=modeled_entry,
        pre_entry_original_quantity=original_quantity,
        pre_entry_adjusted_quantity=adjusted_quantity,
        pre_entry_quote_resized=strategy.pre_entry_quote_resized,
        pre_entry_quantity_retention_fraction=retention,
        actual_average_entry_price=position.average_price,
        actual_filled_quantity=position.size,
        actual_fill_notional_usdt=position.average_price * position.size,
        actual_fill_adverse_slippage_bps_vs_modeled_entry=adverse_slippage_bps,
        account_taker_fee_rate=cycle.account_taker_fee_rate,
        exit_mode=cycle.exit_mode,
        runner_admission_reasons=cycle.runner_admission_reasons,
        liquidation_safety_reason=cycle.liquidation_safety_reason,
        stop_to_liquidation_r=cycle.stop_to_liquidation_r,
        effective_account_equity_usdt=account.effective_session_equity_usdt,
        effective_peak_equity_usdt=account.effective_peak_equity_usdt,
        margin_mode=account.margin_mode,
    )
