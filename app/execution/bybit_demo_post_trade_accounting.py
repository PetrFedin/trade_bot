from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.execution.bybit_demo_account_pnl_reconciliation import (
    BybitDemoAccountPnlReconciliation,
    reconcile_bybit_demo_account_pnl,
)
from app.execution.bybit_demo_funding_reconciliation import (
    BybitDemoAllInPnlReconciliation,
    BybitDemoFundingLedgerWindow,
    apply_funding_to_account_view,
    reconcile_bybit_demo_funding,
)
from app.execution.bybit_demo_lifecycle_gate import (
    BybitDemoLifecycleDecision,
    BybitDemoLifecyclePolicy,
    evaluate_bybit_demo_lifecycle,
)
from app.execution.bybit_demo_trade_monitor import BybitDemoTradeMonitorResult


class _ClosedPnlReader(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool

    def get_closed_pnl(
        self,
        *,
        symbol: str,
        limit: int = 100,
        max_pages: int = 10,
    ) -> tuple[object, ...]: ...


@dataclass(frozen=True)
class BybitDemoPostTradeAccountingResult:
    trade: BybitDemoTradeMonitorResult
    account_pnl: BybitDemoAccountPnlReconciliation | None
    all_in_pnl: BybitDemoAllInPnlReconciliation | None
    lifecycle: BybitDemoLifecycleDecision
    closed_pnl_read_attempted: bool
    funding_ledger_supplied: bool
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def reconcile_bybit_demo_post_trade_accounting(
    trade: BybitDemoTradeMonitorResult,
    *,
    client: _ClosedPnlReader,
    funding_ledger: BybitDemoFundingLedgerWindow | None = None,
    lifecycle_policy: BybitDemoLifecyclePolicy | None = None,
) -> BybitDemoPostTradeAccountingResult:
    """Run the fail-closed post-trade accounting chain for one demo symbol."""

    if client.live_mainnet_order_routing_allowed:
        raise ValueError("post-trade accounting rejected mainnet-capable reader")
    if client.order_writes_supported:
        raise ValueError("post-trade accounting reader must not support order writes")

    if not trade.terminal:
        lifecycle = evaluate_bybit_demo_lifecycle(
            trade,
            None,
            policy=lifecycle_policy,
        )
        return BybitDemoPostTradeAccountingResult(
            trade=trade,
            account_pnl=None,
            all_in_pnl=None,
            lifecycle=lifecycle,
            closed_pnl_read_attempted=False,
            funding_ledger_supplied=funding_ledger is not None,
        )

    rows = client.get_closed_pnl(symbol=trade.symbol, limit=100, max_pages=10)
    mappings = tuple(_require_mapping(row) for row in rows)
    account = reconcile_bybit_demo_account_pnl(trade, mappings)
    all_in = None
    account_for_lifecycle = account
    if funding_ledger is not None:
        all_in = reconcile_bybit_demo_funding(account, funding_ledger)
        account_for_lifecycle = apply_funding_to_account_view(account, all_in)
    lifecycle = evaluate_bybit_demo_lifecycle(
        trade,
        account_for_lifecycle,
        policy=lifecycle_policy,
    )
    return BybitDemoPostTradeAccountingResult(
        trade=trade,
        account_pnl=account,
        all_in_pnl=all_in,
        lifecycle=lifecycle,
        closed_pnl_read_attempted=True,
        funding_ledger_supplied=funding_ledger is not None,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )


def _require_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Bybit demo closed-PnL row must be an object")
    return value
