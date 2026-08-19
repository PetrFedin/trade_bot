from decimal import Decimal

from app.execution.bybit_demo_account_pnl_reconciliation import (
    BybitDemoAccountPnlStatus,
    reconcile_bybit_demo_account_pnl,
)
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
)


def _terminal_trade() -> BybitDemoTradeMonitorResult:
    return BybitDemoTradeMonitorResult(
        status=BybitDemoTradeMonitorStatus.CLOSED_RECONCILED,
        symbol="BTCUSDT",
        entry_order_link_id="ASTRA-DEMO-E-ABC123",
        entry_side="Buy",
        entry_quantity=Decimal("1"),
        exit_quantity=Decimal("1"),
        remaining_quantity=Decimal("0"),
        average_entry_price=Decimal("100"),
        average_exit_price=Decimal("103"),
        entry_fees_usdt=Decimal("0.06"),
        exit_fees_usdt=Decimal("0.0618"),
        execution_fees_usdt=Decimal("0.1218"),
        realized_gross_pnl_usdt=Decimal("3"),
        realized_net_pnl_after_execution_fees_usdt=Decimal("2.8782"),
        reasons=(),
        terminal=True,
        next_entry_allowed=True,
    )


def _row(*, closed_pnl: str = "2.8782") -> dict[str, str]:
    return {
        "symbol": "BTCUSDT",
        "side": "Buy",
        "qty": "1",
        "avgEntryPrice": "100",
        "avgExitPrice": "103",
        "closedPnl": closed_pnl,
        "openFee": "0.06",
        "closeFee": "0.0618",
        "createdTime": "1000",
        "updatedTime": "2000",
    }


def test_terminal_fill_trade_can_reconcile_closed_pnl_but_not_funding() -> None:
    result = reconcile_bybit_demo_account_pnl(_terminal_trade(), [_row()])

    assert result.status is BybitDemoAccountPnlStatus.CLOSED_PNL_RECONCILED
    assert result.account_closed_pnl_reconciled is True
    assert result.account_minus_fill_net_usdt == Decimal("0.0000")
    assert result.execution_fee_difference_usdt == Decimal("0.0000")
    assert result.funding_reconciled is False
    assert result.fully_reconciled_net_pnl is False
    assert result.next_entry_allowed is True
    assert result.strategy_promotion_allowed is False
    assert result.live_mainnet_order_routing_allowed is False


def test_closed_pnl_difference_is_exposed_not_silently_accepted() -> None:
    result = reconcile_bybit_demo_account_pnl(
        _terminal_trade(),
        [_row(closed_pnl="2.50")],
    )

    assert result.status is BybitDemoAccountPnlStatus.CLOSED_PNL_MISMATCH
    assert result.account_closed_pnl_reconciled is False
    assert result.account_minus_fill_net_usdt == Decimal("-0.3782")
    assert "ACCOUNT_CLOSED_PNL_DIFFERS_FROM_FILL_NET" in result.reasons
    assert result.fully_reconciled_net_pnl is False


def test_nonterminal_trade_never_claims_account_reconciliation() -> None:
    trade = _terminal_trade()
    partial = BybitDemoTradeMonitorResult(
        **{
            **trade.__dict__,
            "status": BybitDemoTradeMonitorStatus.PARTIALLY_CLOSED,
            "terminal": False,
            "next_entry_allowed": False,
        }
    )

    result = reconcile_bybit_demo_account_pnl(partial, [_row()])

    assert result.status is BybitDemoAccountPnlStatus.TRADE_NOT_TERMINAL
    assert result.account_closed_pnl_reconciled is False
    assert result.funding_reconciled is False
    assert result.fully_reconciled_net_pnl is False
    assert result.next_entry_allowed is False


def test_multiple_matching_closed_pnl_rows_fail_closed() -> None:
    result = reconcile_bybit_demo_account_pnl(
        _terminal_trade(),
        [_row(), _row()],
    )

    assert result.status is BybitDemoAccountPnlStatus.CLOSED_PNL_AMBIGUOUS
    assert result.account_closed_pnl_reconciled is False
    assert result.next_entry_allowed is True
    assert result.fully_reconciled_net_pnl is False
