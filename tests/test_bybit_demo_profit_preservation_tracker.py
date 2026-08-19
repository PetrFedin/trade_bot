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


def _snapshot(
    *,
    gross: str,
    fill_net: str,
    account_pnl: str,
    funding: str,
) -> BybitDemoPostTradeAccountingResult:
    gross_value = Decimal(gross)
    fill_value = Decimal(fill_net)
    account_value = Decimal(account_pnl)
    funding_value = Decimal(funding)
    fees = gross_value - fill_value
    trade = BybitDemoTradeMonitorResult(
        status=BybitDemoTradeMonitorStatus.CLOSED_RECONCILED,
        symbol="BTCUSDT",
        entry_order_link_id="ASTRA-DEMO-E-PRESERVE",
        entry_side="Buy",
        entry_quantity=Decimal("1"),
        exit_quantity=Decimal("1"),
        remaining_quantity=Decimal("0"),
        average_entry_price=Decimal("100"),
        average_exit_price=Decimal("101"),
        entry_fees_usdt=fees / Decimal("2"),
        exit_fees_usdt=fees / Decimal("2"),
        execution_fees_usdt=fees,
        realized_gross_pnl_usdt=gross_value,
        realized_net_pnl_after_execution_fees_usdt=fill_value,
        reasons=(),
        terminal=True,
        next_entry_allowed=True,
    )
    account = BybitDemoAccountPnlReconciliation(
        status=BybitDemoAccountPnlStatus.CLOSED_PNL_RECONCILED,
        symbol="BTCUSDT",
        matched_record=None,
        fill_net_after_execution_fees_usdt=fill_value,
        account_closed_pnl_usdt=account_value,
        account_minus_fill_net_usdt=account_value - fill_value,
        execution_fee_difference_usdt=Decimal("0"),
        reasons=(),
        account_closed_pnl_reconciled=True,
        funding_reconciled=True,
        fully_reconciled_net_pnl=True,
        next_entry_allowed=True,
    )
    all_in = BybitDemoAllInPnlReconciliation(
        status=BybitDemoFundingStatus.FUNDING_RECONCILED,
        symbol="BTCUSDT",
        account_closed_pnl_usdt=account_value,
        funding_net_usdt=funding_value,
        all_in_net_pnl_usdt=account_value + funding_value,
        funding_entry_count=1 if funding_value else 0,
        reasons=(),
        account_closed_pnl_reconciled=True,
        funding_reconciled=True,
        fully_reconciled_net_pnl=True,
    )
    lifecycle = BybitDemoLifecycleDecision(
        status=BybitDemoLifecycleStatus.FULLY_RECONCILED,
        reasons=(),
        next_entry_allowed=True,
        trade_terminal=True,
        account_closed_pnl_reconciled=True,
        funding_reconciled=True,
        fully_reconciled_net_pnl=True,
    )
    return BybitDemoPostTradeAccountingResult(
        trade=trade,
        account_pnl=account,
        all_in_pnl=all_in,
        lifecycle=lifecycle,
        closed_pnl_read_attempted=True,
        funding_ledger_supplied=True,
    )


def test_profit_preservation_tracker_attributes_where_positive_pnl_is_lost() -> None:
    snapshots = (
        _snapshot(gross="1", fill_net="-0.1", account_pnl="-0.1", funding="0"),
        _snapshot(gross="2", fill_net="1.5", account_pnl="-0.1", funding="0"),
        _snapshot(gross="3", fill_net="2", account_pnl="2", funding="-3"),
        _snapshot(gross="4", fill_net="3", account_pnl="3", funding="0.5"),
    )

    result = summarize_bybit_demo_trade_quality(snapshots)

    assert result["fully_reconciled_trade_count"] == 4
    assert result["gross_positive_terminal_count"] == 4
    assert result["fill_positive_terminal_count"] == 3
    assert result["gross_positive_fill_nonpositive_count"] == 1
    assert result["fill_positive_account_nonpositive_count"] == 1
    assert result["account_positive_all_in_nonpositive_count"] == 1
    assert result["fill_positive_all_in_nonpositive_count"] == 2
    assert result["gross_positive_all_in_nonpositive_count"] == 3
    assert result["fully_reconciled_total_gross_pnl_usdt"] == 10.0
    assert result["fully_reconciled_total_fill_net_after_execution_fees_usdt"] == 6.4
    assert result["fully_reconciled_total_account_closed_pnl_usdt"] == 4.8
    assert result["fully_reconciled_total_funding_net_usdt"] == -2.5
    assert result["fully_reconciled_total_all_in_net_pnl_usdt"] == 2.3
    assert result["fully_reconciled_gross_to_fill_fee_erosion_usdt"] == 3.6
    assert result["fully_reconciled_fill_to_account_delta_usdt"] == -1.6
    assert result["fully_reconciled_account_to_all_in_delta_usdt"] == -2.5
    assert result["fully_reconciled_gross_to_all_in_erosion_usdt"] == 7.7
    assert result["positive_gross_to_positive_all_in_ratio"] == 0.35
    assert result["profit_preservation_diagnostics_only"] is True
    assert result["exit_threshold_retuning_allowed"] is False
    assert result["strategy_promotion_allowed"] is False
    assert result["live_mainnet_order_routing_allowed"] is False
