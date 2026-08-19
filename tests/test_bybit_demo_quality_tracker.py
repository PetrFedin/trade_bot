from decimal import Decimal

from app.execution.bybit_demo_account_pnl_reconciliation import (
    BybitDemoAccountPnlReconciliation,
    BybitDemoAccountPnlStatus,
)
from app.execution.bybit_demo_funding_reconciliation import (
    BybitDemoAllInPnlReconciliation,
    BybitDemoFundingStatus,
)
from app.execution.bybit_demo_lifecycle_gate import (
    BybitDemoLifecycleDecision,
    BybitDemoLifecycleStatus,
)
from app.execution.bybit_demo_post_trade_accounting import (
    BybitDemoPostTradeAccountingResult,
)
from app.execution.bybit_demo_quality_tracker import summarize_bybit_demo_trade_quality
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
)


def _trade(*, fill_net: Decimal, terminal: bool = True) -> BybitDemoTradeMonitorResult:
    return BybitDemoTradeMonitorResult(
        status=(
            BybitDemoTradeMonitorStatus.CLOSED_RECONCILED
            if terminal
            else BybitDemoTradeMonitorStatus.OPEN
        ),
        symbol="BTCUSDT",
        entry_order_link_id="ASTRA-DEMO-E-ABC123",
        entry_side="Buy",
        entry_quantity=Decimal("1"),
        exit_quantity=Decimal("1") if terminal else Decimal("0"),
        remaining_quantity=Decimal("0") if terminal else Decimal("1"),
        average_entry_price=Decimal("100"),
        average_exit_price=Decimal("103") if terminal else None,
        entry_fees_usdt=Decimal("0.06"),
        exit_fees_usdt=Decimal("0.0618") if terminal else Decimal("0"),
        execution_fees_usdt=Decimal("0.1218") if terminal else Decimal("0.06"),
        realized_gross_pnl_usdt=(fill_net + Decimal("0.1218") if terminal else None),
        realized_net_pnl_after_execution_fees_usdt=fill_net if terminal else None,
        reasons=(),
        terminal=terminal,
        next_entry_allowed=terminal,
    )


def _account(*, account_pnl: Decimal) -> BybitDemoAccountPnlReconciliation:
    return BybitDemoAccountPnlReconciliation(
        status=BybitDemoAccountPnlStatus.CLOSED_PNL_RECONCILED,
        symbol="BTCUSDT",
        matched_record=None,
        fill_net_after_execution_fees_usdt=account_pnl,
        account_closed_pnl_usdt=account_pnl,
        account_minus_fill_net_usdt=Decimal("0"),
        execution_fee_difference_usdt=Decimal("0"),
        reasons=(),
        account_closed_pnl_reconciled=True,
        funding_reconciled=True,
        fully_reconciled_net_pnl=True,
        next_entry_allowed=True,
    )


def _all_in(*, account_pnl: Decimal, funding: Decimal) -> BybitDemoAllInPnlReconciliation:
    return BybitDemoAllInPnlReconciliation(
        status=BybitDemoFundingStatus.FUNDING_RECONCILED,
        symbol="BTCUSDT",
        account_closed_pnl_usdt=account_pnl,
        funding_net_usdt=funding,
        all_in_net_pnl_usdt=account_pnl + funding,
        funding_entry_count=1 if funding else 0,
        reasons=(),
        account_closed_pnl_reconciled=True,
        funding_reconciled=True,
        fully_reconciled_net_pnl=True,
    )


def _lifecycle(*, complete: bool) -> BybitDemoLifecycleDecision:
    return BybitDemoLifecycleDecision(
        status=(
            BybitDemoLifecycleStatus.FULLY_RECONCILED
            if complete
            else BybitDemoLifecycleStatus.ACCOUNT_PNL_RECONCILED_FUNDING_PENDING
        ),
        reasons=() if complete else ("FUNDING_RECONCILIATION_PENDING",),
        next_entry_allowed=complete,
        trade_terminal=True,
        account_closed_pnl_reconciled=True,
        funding_reconciled=complete,
        fully_reconciled_net_pnl=complete,
    )


def _snapshot(
    *,
    fill_net: Decimal,
    funding: Decimal | None,
    funding_error: str | None = None,
) -> BybitDemoPostTradeAccountingResult:
    account = _account(account_pnl=fill_net)
    all_in = None if funding is None else _all_in(account_pnl=fill_net, funding=funding)
    return BybitDemoPostTradeAccountingResult(
        trade=_trade(fill_net=fill_net),
        account_pnl=account,
        all_in_pnl=all_in,
        lifecycle=_lifecycle(complete=all_in is not None),
        closed_pnl_read_attempted=True,
        funding_ledger_supplied=False,
        funding_transaction_log_read_attempted=True,
        funding_transaction_log_row_count=0 if funding is None else 1,
        funding_ledger_source=None if funding is None else "BYBIT_TRANSACTION_LOG",
        funding_transaction_log_error_type=funding_error,
    )


def test_quality_tracker_uses_all_in_outcome_and_counts_profit_flip() -> None:
    snapshots = (
        _snapshot(fill_net=Decimal("2.00"), funding=Decimal("-0.20")),
        _snapshot(fill_net=Decimal("1.00"), funding=Decimal("-1.50")),
        _snapshot(fill_net=Decimal("3.00"), funding=None, funding_error="RuntimeError"),
    )

    result = summarize_bybit_demo_trade_quality(snapshots)

    assert result["snapshot_count"] == 3
    assert result["terminal_trade_count"] == 3
    assert result["fully_reconciled_trade_count"] == 2
    assert result["all_in_accounting_pending_count"] == 1
    assert result["fully_reconciled_profit_count"] == 1
    assert result["fully_reconciled_loss_count"] == 1
    assert result["fully_reconciled_profit_fraction"] == 0.5
    assert result["fill_positive_terminal_count"] == 3
    assert result["fill_positive_all_in_nonpositive_count"] == 1
    assert result["fully_reconciled_total_funding_net_usdt"] == -1.7
    assert result["fully_reconciled_total_all_in_net_pnl_usdt"] == 1.3
    assert result["funding_read_error_type_counts"] == {"RuntimeError": 1}
    assert result["final_profit_classification_uses_fully_reconciled_all_in_pnl_only"] is True
    assert result["provisional_fill_pnl_may_mark_trade_profitable"] is False
    assert result["live_mainnet_order_routing_allowed"] is False


def test_quality_tracker_handles_open_and_empty_series() -> None:
    open_snapshot = BybitDemoPostTradeAccountingResult(
        trade=_trade(fill_net=Decimal("0"), terminal=False),
        account_pnl=None,
        all_in_pnl=None,
        lifecycle=BybitDemoLifecycleDecision(
            status=BybitDemoLifecycleStatus.TRADE_NOT_TERMINAL,
            reasons=("FILL_LEVEL_TRADE_NOT_TERMINAL",),
            next_entry_allowed=False,
            trade_terminal=False,
            account_closed_pnl_reconciled=False,
            funding_reconciled=False,
            fully_reconciled_net_pnl=False,
        ),
        closed_pnl_read_attempted=False,
        funding_ledger_supplied=False,
    )

    open_result = summarize_bybit_demo_trade_quality((open_snapshot,))
    empty_result = summarize_bybit_demo_trade_quality(())

    assert open_result["open_trade_count"] == 1
    assert open_result["terminal_trade_count"] == 0
    assert open_result["fully_reconciled_profit_fraction"] is None
    assert empty_result["snapshot_count"] == 0
    assert empty_result["fully_reconciled_total_all_in_net_pnl_usdt"] == 0.0
    assert empty_result["fully_reconciled_profit_fraction"] is None
