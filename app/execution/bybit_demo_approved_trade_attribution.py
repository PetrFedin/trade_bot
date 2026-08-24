from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.execution.bybit_demo_approval_lineage import (
    BybitDemoApprovedEntryAuthorization,
    validate_bybit_demo_approved_entry_authorization,
)
from app.execution.bybit_demo_trade_attribution import BybitDemoTradeAttribution


@dataclass(frozen=True)
class BybitDemoApprovedTradeAttribution:
    approval_id: str
    source_snapshot_id: str
    source_evidence_rank: int
    source_market_rank: int
    entry_order_link_id: str
    terminal_record_sha256: str
    symbol: str
    side: str
    decision_time: str
    selected_signal_rank: int
    execution_fees_usdt: Decimal
    funding_net_usdt: Decimal
    all_in_net_pnl_usdt: Decimal
    all_in_edge_realization_fraction: Decimal
    all_in_r_multiple: Decimal
    fully_reconciled_all_in: bool = True
    operator_approval_required: bool = True
    diagnostics_only: bool = True
    realized_pnl_used_for_online_selection: bool = False
    automatic_selector_retuning_allowed: bool = False
    automatic_exit_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def build_bybit_demo_approved_trade_attribution(
    authorization: BybitDemoApprovedEntryAuthorization,
    attribution: BybitDemoTradeAttribution,
) -> BybitDemoApprovedTradeAttribution:
    """Attach post-trade all-in results to the exact pre-submit evidence authorization."""

    validate_bybit_demo_approved_entry_authorization(authorization)
    _validate_attribution(attribution)
    if attribution.entry_order_link_id != authorization.expected_entry_order_link_id:
        raise ValueError("approved trade attribution entry orderLinkId mismatch")
    if attribution.symbol != authorization.symbol:
        raise ValueError("approved trade attribution symbol mismatch")
    if attribution.side.value != authorization.side:
        raise ValueError("approved trade attribution side mismatch")
    return BybitDemoApprovedTradeAttribution(
        approval_id=authorization.approval_id,
        source_snapshot_id=authorization.source_snapshot_id,
        source_evidence_rank=authorization.source_evidence_rank,
        source_market_rank=authorization.source_market_rank,
        entry_order_link_id=attribution.entry_order_link_id,
        terminal_record_sha256=attribution.terminal_record_sha256,
        symbol=attribution.symbol,
        side=attribution.side.value,
        decision_time=authorization.decision_time,
        selected_signal_rank=attribution.selected_signal_rank,
        execution_fees_usdt=attribution.execution_fees_usdt,
        funding_net_usdt=attribution.funding_net_usdt,
        all_in_net_pnl_usdt=attribution.all_in_net_pnl_usdt,
        all_in_edge_realization_fraction=attribution.all_in_edge_realization_fraction,
        all_in_r_multiple=attribution.all_in_r_multiple,
    )


def _validate_attribution(attribution: BybitDemoTradeAttribution) -> None:
    if attribution.live_mainnet_order_routing_allowed:
        raise ValueError("approved trade attribution rejected mainnet-capable attribution")
    if not attribution.fully_reconciled_all_in or not attribution.diagnostics_only:
        raise ValueError("approved trade attribution requires reconciled diagnostics")
    if attribution.realized_pnl_used_for_online_selection:
        raise ValueError("approved trade attribution cannot feed realized PnL to selection")
    if attribution.automatic_selector_retuning_allowed:
        raise ValueError("approved trade attribution cannot retune selector")
    if attribution.automatic_exit_retuning_allowed:
        raise ValueError("approved trade attribution cannot retune exits")
    if attribution.strategy_promotion_allowed:
        raise ValueError("approved trade attribution cannot promote strategy")


__all__ = [
    "BybitDemoApprovedTradeAttribution",
    "build_bybit_demo_approved_trade_attribution",
]
