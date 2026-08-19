from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.execution.bybit_demo_entry_provenance import BybitDemoEntryDecisionProvenance
from app.execution.bybit_demo_profit_preservation_evidence import (
    BybitDemoProfitPreservationEvidence,
)
from app.execution.bybit_demo_terminal_evidence_store import (
    BybitDemoTerminalEvidenceReceipt,
)
from app.strategy.crypto_perp import CryptoSide

_ZERO = Decimal("0")


@dataclass(frozen=True)
class BybitDemoTradeAttribution:
    entry_order_link_id: str
    terminal_record_sha256: str
    symbol: str
    side: CryptoSide
    selected_signal_rank: int
    executable_candidate_count: int
    selected_after_fallback: bool
    fallback_attempt_count: int
    fallback_stages: tuple[str, ...]
    pre_entry_quote_resized: bool
    pre_entry_quantity_retention_fraction: Decimal | None
    economic_shadow_differs_from_current: bool
    exit_mode: str
    expected_net_edge_usd: Decimal
    risk_budget_usdt: Decimal
    quality_score: Decimal
    actual_fill_adverse_slippage_bps_vs_modeled_entry: Decimal | None
    observed_peak_favorable_r: Decimal
    observed_max_adverse_r: Decimal
    observed_peak_capture_fraction: Decimal | None
    giveback_from_observed_peak_to_exit_r: Decimal
    execution_fees_usdt: Decimal
    funding_net_usdt: Decimal
    all_in_net_pnl_usdt: Decimal
    all_in_edge_realization_fraction: Decimal
    all_in_r_multiple: Decimal
    fully_reconciled_all_in: bool = True
    diagnostics_only: bool = True
    realized_pnl_used_for_online_selection: bool = False
    automatic_selector_retuning_allowed: bool = False
    automatic_exit_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoTradeAttributionPolicy:
    minimum_bucket_sample_size: int = 5

    def validate(self) -> None:
        if self.minimum_bucket_sample_size < 2:
            raise ValueError("trade attribution minimum bucket sample must be at least two")



def build_bybit_demo_trade_attribution(
    provenance: BybitDemoEntryDecisionProvenance,
    evidence: BybitDemoProfitPreservationEvidence,
    *,
    terminal_receipt: BybitDemoTerminalEvidenceReceipt,
) -> BybitDemoTradeAttribution:
    """Join outcome-free entry facts to fully reconciled terminal evidence for diagnostics.

    This function is intentionally post-trade. Realized PnL can be analyzed here because the
    position is already fully reconciled, but the result is explicitly forbidden from feeding
    online selection or automatic threshold changes. The immutable terminal receipt binds the
    attribution to the same entry orderLinkId used by terminal evidence persistence.
    """

    _validate_join(provenance, evidence, terminal_receipt)
    if provenance.expected_net_edge_usd <= 0:
        raise ValueError("trade attribution expected net edge must be positive")
    if provenance.risk_budget_usdt <= 0:
        raise ValueError("trade attribution risk budget must be positive")
    if evidence.all_in_net_pnl_usdt is None:
        raise ValueError("trade attribution requires all-in net PnL")
    funding = _ZERO if evidence.funding_net_usdt is None else evidence.funding_net_usdt
    return BybitDemoTradeAttribution(
        entry_order_link_id=provenance.entry_order_link_id,
        terminal_record_sha256=terminal_receipt.record_sha256,
        symbol=provenance.symbol,
        side=provenance.side,
        selected_signal_rank=provenance.selected_signal_rank,
        executable_candidate_count=provenance.executable_candidate_count,
        selected_after_fallback=provenance.selected_after_fallback,
        fallback_attempt_count=len(provenance.fallback_attempts),
        fallback_stages=tuple(attempt.stage.value for attempt in provenance.fallback_attempts),
        pre_entry_quote_resized=provenance.pre_entry_quote_resized,
        pre_entry_quantity_retention_fraction=(
            provenance.pre_entry_quantity_retention_fraction
        ),
        economic_shadow_differs_from_current=(
            provenance.economic_shadow_differs_from_current
        ),
        exit_mode=provenance.exit_mode,
        expected_net_edge_usd=provenance.expected_net_edge_usd,
        risk_budget_usdt=provenance.risk_budget_usdt,
        quality_score=provenance.quality_score,
        actual_fill_adverse_slippage_bps_vs_modeled_entry=(
            provenance.actual_fill_adverse_slippage_bps_vs_modeled_entry
        ),
        observed_peak_favorable_r=evidence.observed_peak_favorable_r,
        observed_max_adverse_r=evidence.observed_max_adverse_r,
        observed_peak_capture_fraction=evidence.observed_peak_capture_fraction,
        giveback_from_observed_peak_to_exit_r=(
            evidence.giveback_from_observed_peak_to_exit_r
        ),
        execution_fees_usdt=evidence.execution_fees_usdt,
        funding_net_usdt=funding,
        all_in_net_pnl_usdt=evidence.all_in_net_pnl_usdt,
        all_in_edge_realization_fraction=(
            evidence.all_in_net_pnl_usdt / provenance.expected_net_edge_usd
        ),
        all_in_r_multiple=evidence.all_in_net_pnl_usdt / provenance.risk_budget_usdt,
    )


def summarize_bybit_demo_trade_attribution(
    trades: Iterable[BybitDemoTradeAttribution],
    *,
    policy: BybitDemoTradeAttributionPolicy | None = None,
) -> dict[str, Any]:
    active = BybitDemoTradeAttributionPolicy() if policy is None else policy
    active.validate()
    rows = tuple(trades)
    for row in rows:
        _validate_attribution(row)

    return {
        "trade_count": len(rows),
        "overall": _bucket_summary(rows, active),
        "by_side": _group_summary(rows, lambda row: row.side.value, active),
        "by_signal_rank": _group_summary(
            rows,
            lambda row: str(row.selected_signal_rank),
            active,
        ),
        "by_fallback": _group_summary(
            rows,
            lambda row: "FALLBACK" if row.selected_after_fallback else "DIRECT",
            active,
        ),
        "by_pre_entry_resize": _group_summary(
            rows,
            lambda row: "RESIZED" if row.pre_entry_quote_resized else "NOT_RESIZED",
            active,
        ),
        "by_economic_shadow_agreement": _group_summary(
            rows,
            lambda row: (
                "DISAGREES" if row.economic_shadow_differs_from_current else "AGREES"
            ),
            active,
        ),
        "by_exit_mode": _group_summary(rows, lambda row: row.exit_mode, active),
        "minimum_bucket_sample_size": active.minimum_bucket_sample_size,
        "small_sample_buckets_are_descriptive_only": True,
        "diagnostics_only": True,
        "realized_pnl_used_for_online_selection": False,
        "automatic_selector_retuning_allowed": False,
        "automatic_exit_retuning_allowed": False,
        "strategy_promotion_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _validate_join(
    provenance: BybitDemoEntryDecisionProvenance,
    evidence: BybitDemoProfitPreservationEvidence,
    receipt: BybitDemoTerminalEvidenceReceipt,
) -> None:
    if provenance.live_mainnet_order_routing_allowed:
        raise ValueError("trade attribution rejected mainnet-capable provenance")
    if evidence.live_mainnet_order_routing_allowed:
        raise ValueError("trade attribution rejected mainnet-capable terminal evidence")
    if receipt.live_mainnet_order_routing_allowed:
        raise ValueError("trade attribution rejected mainnet-capable terminal receipt")
    if provenance.realized_pnl_used_for_selection:
        raise ValueError("trade attribution provenance leaked realized PnL into selection")
    if not provenance.diagnostics_only or provenance.automatic_selector_retuning_allowed:
        raise ValueError("trade attribution requires diagnostics-only provenance")
    if not evidence.diagnostics_only or evidence.exit_threshold_retuning_allowed:
        raise ValueError("trade attribution requires diagnostics-only terminal evidence")
    if not evidence.fully_reconciled_all_in or evidence.all_in_net_pnl_usdt is None:
        raise ValueError("trade attribution requires fully reconciled all-in evidence")
    if receipt.entry_order_link_id != provenance.entry_order_link_id:
        raise ValueError("trade attribution entry orderLinkId mismatch")
    if evidence.symbol != provenance.symbol or evidence.side is not provenance.side:
        raise ValueError("trade attribution symbol/side mismatch")
    if len(receipt.record_sha256) != 64:
        raise ValueError("trade attribution terminal receipt checksum is invalid")


def _validate_attribution(row: BybitDemoTradeAttribution) -> None:
    if row.live_mainnet_order_routing_allowed:
        raise ValueError("trade attribution row cannot permit live routing")
    if not row.fully_reconciled_all_in or not row.diagnostics_only:
        raise ValueError("trade attribution row must be fully reconciled diagnostics")
    if row.realized_pnl_used_for_online_selection:
        raise ValueError("trade attribution row cannot feed realized PnL to selection")
    if row.automatic_selector_retuning_allowed or row.automatic_exit_retuning_allowed:
        raise ValueError("trade attribution row cannot authorize automatic retuning")
    if row.strategy_promotion_allowed:
        raise ValueError("trade attribution row cannot authorize strategy promotion")
    if row.expected_net_edge_usd <= 0 or row.risk_budget_usdt <= 0:
        raise ValueError("trade attribution row has invalid positive basis")


def _group_summary(
    rows: tuple[BybitDemoTradeAttribution, ...],
    key_fn: Any,
    policy: BybitDemoTradeAttributionPolicy,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[BybitDemoTradeAttribution]] = defaultdict(list)
    for row in rows:
        groups[str(key_fn(row))].append(row)
    return {
        key: _bucket_summary(tuple(values), policy)
        for key, values in sorted(groups.items())
    }


def _bucket_summary(
    rows: tuple[BybitDemoTradeAttribution, ...],
    policy: BybitDemoTradeAttributionPolicy,
) -> dict[str, Any]:
    count = len(rows)
    profits = sum(row.all_in_net_pnl_usdt > 0 for row in rows)
    losses = sum(row.all_in_net_pnl_usdt < 0 for row in rows)
    flats = count - profits - losses
    total_pnl = sum((row.all_in_net_pnl_usdt for row in rows), start=_ZERO)
    gross_profit = sum(
        (row.all_in_net_pnl_usdt for row in rows if row.all_in_net_pnl_usdt > 0),
        start=_ZERO,
    )
    gross_loss = -sum(
        (row.all_in_net_pnl_usdt for row in rows if row.all_in_net_pnl_usdt < 0),
        start=_ZERO,
    )
    profit_factor = None if gross_loss == 0 else gross_profit / gross_loss
    return {
        "trade_count": count,
        "profit_count": profits,
        "loss_count": losses,
        "flat_count": flats,
        "total_all_in_net_pnl_usdt": float(total_pnl),
        "profit_factor": None if profit_factor is None else float(profit_factor),
        "average_all_in_net_pnl_usdt": _average_float(
            row.all_in_net_pnl_usdt for row in rows
        ),
        "average_all_in_edge_realization_fraction": _average_float(
            row.all_in_edge_realization_fraction for row in rows
        ),
        "average_all_in_r_multiple": _average_float(row.all_in_r_multiple for row in rows),
        "average_expected_net_edge_usd": _average_float(
            row.expected_net_edge_usd for row in rows
        ),
        "average_adverse_fill_slippage_bps": _average_optional_float(
            row.actual_fill_adverse_slippage_bps_vs_modeled_entry for row in rows
        ),
        "average_quantity_retention_fraction": _average_optional_float(
            row.pre_entry_quantity_retention_fraction for row in rows
        ),
        "average_observed_peak_favorable_r": _average_float(
            row.observed_peak_favorable_r for row in rows
        ),
        "average_observed_max_adverse_r": _average_float(
            row.observed_max_adverse_r for row in rows
        ),
        "average_peak_capture_fraction": _average_optional_float(
            row.observed_peak_capture_fraction for row in rows
        ),
        "average_giveback_r": _average_float(
            row.giveback_from_observed_peak_to_exit_r for row in rows
        ),
        "total_execution_fees_usdt": float(
            sum((row.execution_fees_usdt for row in rows), start=_ZERO)
        ),
        "total_funding_net_usdt": float(
            sum((row.funding_net_usdt for row in rows), start=_ZERO)
        ),
        "sample_sufficient_for_hypothesis": count >= policy.minimum_bucket_sample_size,
        "automatic_retuning_allowed": False,
    }


def _average_float(values: Iterable[Decimal]) -> float | None:
    rows = tuple(values)
    if not rows:
        return None
    return float(sum(rows, start=_ZERO) / Decimal(len(rows)))


def _average_optional_float(values: Iterable[Decimal | None]) -> float | None:
    rows = tuple(value for value in values if value is not None)
    return _average_float(rows)
