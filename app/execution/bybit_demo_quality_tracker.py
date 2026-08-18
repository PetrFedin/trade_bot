from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.execution.bybit_demo_post_trade_accounting import (
    BybitDemoPostTradeAccountingResult,
    BybitDemoProfitOutcomeStatus,
)

_ZERO = Decimal("0")


def summarize_bybit_demo_trade_quality(
    snapshots: Sequence[BybitDemoPostTradeAccountingResult],
) -> dict[str, Any]:
    """Aggregate demo trade outcomes without promoting provisional PnL to final profit."""

    outcome_counts: Counter[str] = Counter()
    funding_error_counts: Counter[str] = Counter()
    terminal_count = 0
    fully_reconciled_count = 0
    fill_positive_terminal_count = 0
    gross_positive_terminal_count = 0
    gross_positive_fill_nonpositive_count = 0
    fill_positive_account_nonpositive_count = 0
    account_positive_all_in_nonpositive_count = 0
    fill_positive_all_in_nonpositive_count = 0
    gross_positive_all_in_nonpositive_count = 0
    fill_nonpositive_all_in_positive_count = 0
    total_execution_fees = _ZERO
    total_fill_net = _ZERO
    total_account_closed_pnl = _ZERO
    total_funding = _ZERO
    total_all_in = _ZERO
    fully_reconciled_gross = _ZERO
    fully_reconciled_fill_net = _ZERO
    fully_reconciled_account = _ZERO
    positive_gross_denominator = _ZERO
    positive_all_in_from_positive_gross = _ZERO

    for snapshot in snapshots:
        if snapshot.live_mainnet_order_routing_allowed:
            raise ValueError("demo quality tracker rejected mainnet-capable snapshot")
        outcome = snapshot.profit_outcome_status
        outcome_counts[outcome.value] += 1
        if snapshot.funding_transaction_log_error_type:
            funding_error_counts[snapshot.funding_transaction_log_error_type] += 1

        trade = snapshot.trade
        total_execution_fees += trade.execution_fees_usdt
        if not trade.terminal:
            continue
        terminal_count += 1
        gross_pnl = trade.realized_gross_pnl_usdt
        fill_net = trade.realized_net_pnl_after_execution_fees_usdt
        if gross_pnl is not None and gross_pnl > 0:
            gross_positive_terminal_count += 1
        if fill_net is not None:
            total_fill_net += fill_net
            if fill_net > 0:
                fill_positive_terminal_count += 1
        if gross_pnl is not None and fill_net is not None:
            if gross_pnl > 0 and fill_net <= 0:
                gross_positive_fill_nonpositive_count += 1

        account = snapshot.account_pnl
        account_pnl = None
        if account is not None and account.account_closed_pnl_usdt is not None:
            account_pnl = account.account_closed_pnl_usdt
            total_account_closed_pnl += account_pnl
            if fill_net is not None and fill_net > 0 and account_pnl <= 0:
                fill_positive_account_nonpositive_count += 1

        all_in = snapshot.fully_reconciled_all_in_net_pnl_usdt
        if all_in is None:
            continue
        if gross_pnl is None or fill_net is None or account_pnl is None:
            raise ValueError(
                "fully reconciled demo quality snapshot is missing gross/fill/account PnL"
            )
        fully_reconciled_count += 1
        fully_reconciled_gross += gross_pnl
        fully_reconciled_fill_net += fill_net
        fully_reconciled_account += account_pnl
        total_all_in += all_in
        if snapshot.all_in_pnl is not None and snapshot.all_in_pnl.funding_net_usdt is not None:
            total_funding += snapshot.all_in_pnl.funding_net_usdt

        if gross_pnl > 0:
            positive_gross_denominator += gross_pnl
            positive_all_in_from_positive_gross += max(all_in, _ZERO)
            if all_in <= 0:
                gross_positive_all_in_nonpositive_count += 1
        if account_pnl > 0 and all_in <= 0:
            account_positive_all_in_nonpositive_count += 1
        if fill_net > 0 and all_in <= 0:
            fill_positive_all_in_nonpositive_count += 1
        if fill_net <= 0 and all_in > 0:
            fill_nonpositive_all_in_positive_count += 1

    profit_count = outcome_counts[BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_PROFIT.value]
    loss_count = outcome_counts[BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_LOSS.value]
    flat_count = outcome_counts[BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_FLAT.value]
    pending_count = outcome_counts[BybitDemoProfitOutcomeStatus.ALL_IN_ACCOUNTING_PENDING.value]
    open_count = outcome_counts[BybitDemoProfitOutcomeStatus.TRADE_OPEN.value]
    if profit_count + loss_count + flat_count != fully_reconciled_count:
        raise ValueError("demo quality tracker outcome counts do not reconcile")

    gross_to_fill_erosion = fully_reconciled_gross - fully_reconciled_fill_net
    fill_to_account_delta = fully_reconciled_account - fully_reconciled_fill_net
    account_to_all_in_delta = total_all_in - fully_reconciled_account
    gross_to_all_in_erosion = fully_reconciled_gross - total_all_in

    return {
        "qualification": "BYBIT_DEMO_ALL_IN_TRADE_QUALITY_TRACKER",
        "snapshot_count": len(snapshots),
        "open_trade_count": open_count,
        "terminal_trade_count": terminal_count,
        "all_in_accounting_pending_count": pending_count,
        "fully_reconciled_trade_count": fully_reconciled_count,
        "fully_reconciled_profit_count": profit_count,
        "fully_reconciled_loss_count": loss_count,
        "fully_reconciled_flat_count": flat_count,
        "fully_reconciled_profit_fraction": (
            None
            if fully_reconciled_count == 0
            else float(Decimal(profit_count) / Decimal(fully_reconciled_count))
        ),
        "gross_positive_terminal_count": gross_positive_terminal_count,
        "fill_positive_terminal_count": fill_positive_terminal_count,
        "gross_positive_fill_nonpositive_count": gross_positive_fill_nonpositive_count,
        "fill_positive_account_nonpositive_count": fill_positive_account_nonpositive_count,
        "account_positive_all_in_nonpositive_count": (
            account_positive_all_in_nonpositive_count
        ),
        "fill_positive_all_in_nonpositive_count": fill_positive_all_in_nonpositive_count,
        "gross_positive_all_in_nonpositive_count": gross_positive_all_in_nonpositive_count,
        "fill_nonpositive_all_in_positive_count": fill_nonpositive_all_in_positive_count,
        "total_execution_fees_usdt": float(total_execution_fees),
        "provisional_total_fill_net_after_execution_fees_usdt": float(total_fill_net),
        "reconciled_total_account_closed_pnl_usdt": float(total_account_closed_pnl),
        "fully_reconciled_total_gross_pnl_usdt": float(fully_reconciled_gross),
        "fully_reconciled_total_fill_net_after_execution_fees_usdt": float(
            fully_reconciled_fill_net
        ),
        "fully_reconciled_total_account_closed_pnl_usdt": float(
            fully_reconciled_account
        ),
        "fully_reconciled_total_funding_net_usdt": float(total_funding),
        "fully_reconciled_total_all_in_net_pnl_usdt": float(total_all_in),
        "fully_reconciled_gross_to_fill_fee_erosion_usdt": float(
            gross_to_fill_erosion
        ),
        "fully_reconciled_fill_to_account_delta_usdt": float(fill_to_account_delta),
        "fully_reconciled_account_to_all_in_delta_usdt": float(account_to_all_in_delta),
        "fully_reconciled_gross_to_all_in_erosion_usdt": float(gross_to_all_in_erosion),
        "positive_gross_to_positive_all_in_ratio": (
            None
            if positive_gross_denominator <= 0
            else float(positive_all_in_from_positive_gross / positive_gross_denominator)
        ),
        "profit_outcome_counts": dict(sorted(outcome_counts.items())),
        "funding_read_error_type_counts": dict(sorted(funding_error_counts.items())),
        "final_profit_classification_uses_fully_reconciled_all_in_pnl_only": True,
        "provisional_fill_pnl_may_mark_trade_profitable": False,
        "profit_preservation_diagnostics_only": True,
        "exit_threshold_retuning_allowed": False,
        "strategy_promotion_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }
