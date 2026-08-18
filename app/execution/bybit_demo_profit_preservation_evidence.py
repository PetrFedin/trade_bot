from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionFinal
from app.execution.bybit_demo_post_trade_accounting import (
    BybitDemoPostTradeAccountingResult,
    BybitDemoProfitOutcomeStatus,
)
from app.strategy.crypto_perp import CryptoSide

_ZERO = Decimal("0")


@dataclass(frozen=True)
class BybitDemoProfitPreservationEvidence:
    symbol: str
    side: CryptoSide
    observation_count: int
    observed_peak_favorable_r: Decimal
    observed_max_adverse_r: Decimal
    realized_gross_exit_r: Decimal
    observed_peak_capture_fraction: Decimal | None
    giveback_from_observed_peak_to_exit_r: Decimal
    exit_exceeded_observed_peak: bool
    partial_close_seen: bool
    realized_gross_pnl_usdt: Decimal
    realized_net_after_execution_fees_usdt: Decimal
    execution_fees_usdt: Decimal
    account_closed_pnl_usdt: Decimal | None
    funding_net_usdt: Decimal | None
    all_in_net_pnl_usdt: Decimal | None
    profit_outcome_status: BybitDemoProfitOutcomeStatus
    positive_peak_nonpositive_gross_exit: bool
    gross_positive_fill_nonpositive: bool
    fill_positive_account_nonpositive: bool
    account_positive_all_in_nonpositive: bool
    positive_peak_nonpositive_all_in: bool | None
    fully_reconciled_all_in: bool
    diagnostics_only: bool = True
    exit_threshold_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def build_bybit_demo_profit_preservation_evidence(
    excursion: BybitDemoTradeExcursionFinal,
    accounting: BybitDemoPostTradeAccountingResult,
) -> BybitDemoProfitPreservationEvidence:
    """Join observed in-trade excursion quality to terminal all-in accounting.

    This evidence answers *where* a favorable trade lost value: before gross exit, in execution
    fees, in account closed-PnL reconciliation, or in funding. It is intentionally diagnostic and
    cannot authorize post-hoc stop/trailing retuning or strategy promotion.
    """

    if excursion.live_mainnet_order_routing_allowed:
        raise ValueError("profit-preservation evidence rejected live excursion input")
    if accounting.live_mainnet_order_routing_allowed:
        raise ValueError("profit-preservation evidence rejected live accounting input")
    trade = accounting.trade
    if not trade.terminal:
        raise ValueError("profit-preservation evidence requires a terminal trade")
    if trade.symbol != excursion.symbol:
        raise ValueError("profit-preservation evidence symbol mismatch")
    expected_entry_side = "Buy" if excursion.side is CryptoSide.LONG else "Sell"
    if trade.entry_side != expected_entry_side:
        raise ValueError("profit-preservation evidence side mismatch")
    if trade.realized_gross_pnl_usdt is None:
        raise ValueError("terminal profit-preservation evidence requires gross PnL")
    if trade.realized_net_pnl_after_execution_fees_usdt is None:
        raise ValueError("terminal profit-preservation evidence requires fill net PnL")

    gross = trade.realized_gross_pnl_usdt
    fill_net = trade.realized_net_pnl_after_execution_fees_usdt
    _validate_exit_sign(excursion.realized_gross_exit_r, gross)

    account_pnl = None
    if accounting.account_pnl is not None:
        account_pnl = accounting.account_pnl.account_closed_pnl_usdt
    funding = None
    if accounting.all_in_pnl is not None:
        funding = accounting.all_in_pnl.funding_net_usdt
    all_in = accounting.fully_reconciled_all_in_net_pnl_usdt
    fully_reconciled = all_in is not None
    if fully_reconciled and account_pnl is None:
        raise ValueError("fully reconciled profit evidence is missing account closed PnL")

    positive_peak = excursion.observed_peak_favorable_r > 0
    return BybitDemoProfitPreservationEvidence(
        symbol=excursion.symbol,
        side=excursion.side,
        observation_count=excursion.observation_count,
        observed_peak_favorable_r=excursion.observed_peak_favorable_r,
        observed_max_adverse_r=excursion.observed_max_adverse_r,
        realized_gross_exit_r=excursion.realized_gross_exit_r,
        observed_peak_capture_fraction=excursion.observed_peak_capture_fraction,
        giveback_from_observed_peak_to_exit_r=(
            excursion.giveback_from_observed_peak_to_exit_r
        ),
        exit_exceeded_observed_peak=excursion.exit_exceeded_observed_peak,
        partial_close_seen=excursion.partial_close_seen,
        realized_gross_pnl_usdt=gross,
        realized_net_after_execution_fees_usdt=fill_net,
        execution_fees_usdt=trade.execution_fees_usdt,
        account_closed_pnl_usdt=account_pnl,
        funding_net_usdt=funding,
        all_in_net_pnl_usdt=all_in,
        profit_outcome_status=accounting.profit_outcome_status,
        positive_peak_nonpositive_gross_exit=(positive_peak and gross <= 0),
        gross_positive_fill_nonpositive=(gross > 0 and fill_net <= 0),
        fill_positive_account_nonpositive=(
            fill_net > 0 and account_pnl is not None and account_pnl <= 0
        ),
        account_positive_all_in_nonpositive=(
            account_pnl is not None
            and account_pnl > 0
            and all_in is not None
            and all_in <= 0
        ),
        positive_peak_nonpositive_all_in=(
            None if all_in is None else positive_peak and all_in <= 0
        ),
        fully_reconciled_all_in=fully_reconciled,
    )


def summarize_bybit_demo_profit_preservation_evidence(
    evidence: Sequence[BybitDemoProfitPreservationEvidence],
) -> dict[str, Any]:
    """Aggregate end-to-end profit preservation without changing exit policy."""

    outcome_counts: Counter[str] = Counter()
    side_counts: Counter[str] = Counter()
    reconciled = [item for item in evidence if item.fully_reconciled_all_in]
    positive_peak = [item for item in evidence if item.observed_peak_favorable_r > 0]
    capture = [
        item.observed_peak_capture_fraction
        for item in positive_peak
        if item.observed_peak_capture_fraction is not None
    ]

    for item in evidence:
        if item.live_mainnet_order_routing_allowed:
            raise ValueError("profit-preservation summary rejected live evidence")
        if not item.diagnostics_only or item.exit_threshold_retuning_allowed:
            raise ValueError("profit-preservation evidence lost diagnostics-only contract")
        outcome_counts[item.profit_outcome_status.value] += 1
        side_counts[item.side.value] += 1

    total_gross = sum((item.realized_gross_pnl_usdt for item in reconciled), start=_ZERO)
    total_fill = sum(
        (item.realized_net_after_execution_fees_usdt for item in reconciled),
        start=_ZERO,
    )
    total_account = sum(
        (
            item.account_closed_pnl_usdt
            for item in reconciled
            if item.account_closed_pnl_usdt is not None
        ),
        start=_ZERO,
    )
    total_funding = sum(
        (
            item.funding_net_usdt
            for item in reconciled
            if item.funding_net_usdt is not None
        ),
        start=_ZERO,
    )
    total_all_in = sum(
        (
            item.all_in_net_pnl_usdt
            for item in reconciled
            if item.all_in_net_pnl_usdt is not None
        ),
        start=_ZERO,
    )

    return {
        "qualification": "BYBIT_DEMO_END_TO_END_PROFIT_PRESERVATION_EVIDENCE",
        "trade_count": len(evidence),
        "fully_reconciled_all_in_count": len(reconciled),
        "accounting_pending_count": len(evidence) - len(reconciled),
        "positive_observed_peak_trade_count": len(positive_peak),
        "positive_peak_nonpositive_gross_exit_count": sum(
            item.positive_peak_nonpositive_gross_exit for item in positive_peak
        ),
        "positive_peak_nonpositive_all_in_count": sum(
            item.positive_peak_nonpositive_all_in is True for item in positive_peak
        ),
        "gross_positive_fill_nonpositive_count": sum(
            item.gross_positive_fill_nonpositive for item in evidence
        ),
        "fill_positive_account_nonpositive_count": sum(
            item.fill_positive_account_nonpositive for item in evidence
        ),
        "account_positive_all_in_nonpositive_count": sum(
            item.account_positive_all_in_nonpositive for item in evidence
        ),
        "partial_close_seen_count": sum(item.partial_close_seen for item in evidence),
        "average_observed_peak_favorable_r": _average(
            [item.observed_peak_favorable_r for item in evidence]
        ),
        "average_observed_max_adverse_r": _average(
            [item.observed_max_adverse_r for item in evidence]
        ),
        "average_observed_peak_capture_fraction": _average(capture),
        "average_giveback_from_observed_peak_to_exit_r": _average(
            [item.giveback_from_observed_peak_to_exit_r for item in positive_peak]
        ),
        "fully_reconciled_total_gross_pnl_usdt": float(total_gross),
        "fully_reconciled_total_fill_net_after_execution_fees_usdt": float(total_fill),
        "fully_reconciled_total_account_closed_pnl_usdt": float(total_account),
        "fully_reconciled_total_funding_net_usdt": float(total_funding),
        "fully_reconciled_total_all_in_net_pnl_usdt": float(total_all_in),
        "fully_reconciled_gross_to_all_in_erosion_usdt": float(
            total_gross - total_all_in
        ),
        "profit_outcome_counts": dict(sorted(outcome_counts.items())),
        "side_counts": dict(sorted(side_counts.items())),
        "observed_peak_is_sampling_lower_bound": True,
        "all_in_profit_is_required_for_final_profit_classification": True,
        "diagnostics_only": True,
        "exit_threshold_retuning_allowed": False,
        "strategy_promotion_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _validate_exit_sign(exit_r: Decimal, gross_pnl: Decimal) -> None:
    if exit_r > 0 and gross_pnl <= 0:
        raise ValueError("positive gross exit R conflicts with non-positive realized gross PnL")
    if exit_r < 0 and gross_pnl >= 0:
        raise ValueError("negative gross exit R conflicts with non-negative realized gross PnL")
    if exit_r == 0 and gross_pnl != 0:
        raise ValueError("flat gross exit R conflicts with non-zero realized gross PnL")


def _average(values: Sequence[Decimal]) -> float | None:
    if not values:
        return None
    return float(sum(values, start=_ZERO) / Decimal(len(values)))
