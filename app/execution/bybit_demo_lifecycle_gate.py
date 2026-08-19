from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.execution.bybit_demo_account_pnl_reconciliation import (
    BybitDemoAccountPnlReconciliation,
    BybitDemoAccountPnlStatus,
)
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
)


class BybitDemoLifecycleStatus(StrEnum):
    TRADE_NOT_TERMINAL = "TRADE_NOT_TERMINAL"
    ACCOUNT_PNL_PENDING = "ACCOUNT_PNL_PENDING"
    ACCOUNT_PNL_MISMATCH = "ACCOUNT_PNL_MISMATCH"
    ACCOUNT_PNL_RECONCILED_FUNDING_PENDING = "ACCOUNT_PNL_RECONCILED_FUNDING_PENDING"
    FULLY_RECONCILED = "FULLY_RECONCILED"


@dataclass(frozen=True)
class BybitDemoLifecyclePolicy:
    require_account_closed_pnl_before_next_entry: bool = True
    require_funding_before_next_entry: bool = True


@dataclass(frozen=True)
class BybitDemoLifecycleDecision:
    status: BybitDemoLifecycleStatus
    reasons: tuple[str, ...]
    next_entry_allowed: bool
    trade_terminal: bool
    account_closed_pnl_reconciled: bool
    funding_reconciled: bool
    fully_reconciled_net_pnl: bool
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def evaluate_bybit_demo_lifecycle(
    trade: BybitDemoTradeMonitorResult,
    account_pnl: BybitDemoAccountPnlReconciliation | None,
    *,
    policy: BybitDemoLifecyclePolicy | None = None,
) -> BybitDemoLifecycleDecision:
    """Decide whether one demo symbol is safe to recycle after a completed trade."""

    active = BybitDemoLifecyclePolicy() if policy is None else policy
    terminal = (
        trade.status is BybitDemoTradeMonitorStatus.CLOSED_RECONCILED
        and trade.terminal
        and trade.next_entry_allowed
    )
    if not terminal:
        return _decision(
            BybitDemoLifecycleStatus.TRADE_NOT_TERMINAL,
            reasons=("FILL_LEVEL_TRADE_NOT_TERMINAL",),
            next_entry_allowed=False,
            trade_terminal=False,
            account_reconciled=False,
            funding_reconciled=False,
        )

    if account_pnl is None:
        allow = not active.require_account_closed_pnl_before_next_entry
        return _decision(
            BybitDemoLifecycleStatus.ACCOUNT_PNL_PENDING,
            reasons=("ACCOUNT_CLOSED_PNL_RECONCILIATION_PENDING",),
            next_entry_allowed=allow,
            trade_terminal=True,
            account_reconciled=False,
            funding_reconciled=False,
        )

    account_reconciled = (
        account_pnl.status is BybitDemoAccountPnlStatus.CLOSED_PNL_RECONCILED
        and account_pnl.account_closed_pnl_reconciled
    )
    if not account_reconciled:
        return _decision(
            BybitDemoLifecycleStatus.ACCOUNT_PNL_MISMATCH,
            reasons=(
                "ACCOUNT_CLOSED_PNL_NOT_RECONCILED",
                *account_pnl.reasons,
            ),
            next_entry_allowed=(
                not active.require_account_closed_pnl_before_next_entry
            ),
            trade_terminal=True,
            account_reconciled=False,
            funding_reconciled=account_pnl.funding_reconciled,
        )

    if not account_pnl.funding_reconciled:
        return _decision(
            BybitDemoLifecycleStatus.ACCOUNT_PNL_RECONCILED_FUNDING_PENDING,
            reasons=("FUNDING_RECONCILIATION_PENDING",),
            next_entry_allowed=not active.require_funding_before_next_entry,
            trade_terminal=True,
            account_reconciled=True,
            funding_reconciled=False,
        )

    return _decision(
        BybitDemoLifecycleStatus.FULLY_RECONCILED,
        reasons=(),
        next_entry_allowed=True,
        trade_terminal=True,
        account_reconciled=True,
        funding_reconciled=True,
    )


def _decision(
    status: BybitDemoLifecycleStatus,
    *,
    reasons: tuple[str, ...],
    next_entry_allowed: bool,
    trade_terminal: bool,
    account_reconciled: bool,
    funding_reconciled: bool,
) -> BybitDemoLifecycleDecision:
    return BybitDemoLifecycleDecision(
        status=status,
        reasons=tuple(dict.fromkeys(reasons)),
        next_entry_allowed=next_entry_allowed,
        trade_terminal=trade_terminal,
        account_closed_pnl_reconciled=account_reconciled,
        funding_reconciled=funding_reconciled,
        fully_reconciled_net_pnl=account_reconciled and funding_reconciled,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )
