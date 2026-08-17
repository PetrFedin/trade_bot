from decimal import Decimal

import pytest

from app.execution.bybit_demo_funding_reconciliation import (
    BybitDemoFundingLedgerEntry,
    BybitDemoFundingLedgerWindow,
)
from app.execution.bybit_demo_lifecycle_gate import (
    BybitDemoLifecyclePolicy,
    BybitDemoLifecycleStatus,
)
from app.execution.bybit_demo_post_trade_accounting import (
    reconcile_bybit_demo_post_trade_accounting,
)
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
)


class _Reader:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, rows: tuple[object, ...]) -> None:
        self.rows = rows
        self.calls = 0

    def get_closed_pnl(
        self,
        *,
        symbol: str,
        limit: int = 100,
        max_pages: int = 10,
    ) -> tuple[object, ...]:
        assert symbol == "BTCUSDT"
        assert limit == 100
        assert max_pages == 10
        self.calls += 1
        return self.rows


def _trade(*, terminal: bool = True) -> BybitDemoTradeMonitorResult:
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
        realized_gross_pnl_usdt=Decimal("3") if terminal else None,
        realized_net_pnl_after_execution_fees_usdt=(
            Decimal("2.8782") if terminal else None
        ),
        reasons=(),
        terminal=terminal,
        next_entry_allowed=terminal,
    )


def _closed_pnl() -> dict[str, str]:
    return {
        "symbol": "BTCUSDT",
        "side": "Buy",
        "qty": "1",
        "avgEntryPrice": "100",
        "avgExitPrice": "103",
        "closedPnl": "2.8782",
        "openFee": "0.06",
        "closeFee": "0.0618",
        "createdTime": "1000",
        "updatedTime": "5000",
    }


def test_terminal_trade_runs_closed_pnl_reconciliation_and_unlocks_default_lifecycle() -> None:
    reader = _Reader((_closed_pnl(),))

    result = reconcile_bybit_demo_post_trade_accounting(
        _trade(),
        client=reader,
    )

    assert reader.calls == 1
    assert result.closed_pnl_read_attempted is True
    assert result.account_pnl is not None
    assert result.account_pnl.account_closed_pnl_reconciled is True
    assert result.all_in_pnl is None
    assert result.lifecycle.status is (
        BybitDemoLifecycleStatus.ACCOUNT_PNL_RECONCILED_FUNDING_PENDING
    )
    assert result.lifecycle.next_entry_allowed is True
    assert result.lifecycle.fully_reconciled_net_pnl is False
    assert result.live_mainnet_order_routing_allowed is False


def test_strict_funding_policy_unlocks_only_after_covered_funding_ledger() -> None:
    reader = _Reader((_closed_pnl(),))
    strict = BybitDemoLifecyclePolicy(require_funding_before_next_entry=True)
    pending = reconcile_bybit_demo_post_trade_accounting(
        _trade(),
        client=reader,
        lifecycle_policy=strict,
    )
    ledger = BybitDemoFundingLedgerWindow(
        coverage_start_ms=0,
        coverage_end_ms=6000,
        entries=(
            BybitDemoFundingLedgerEntry(
                symbol="BTCUSDT",
                transaction_time_ms=3000,
                amount_usdt=Decimal("-0.10"),
                reference_id="funding-1",
            ),
        ),
    )
    complete = reconcile_bybit_demo_post_trade_accounting(
        _trade(),
        client=reader,
        funding_ledger=ledger,
        lifecycle_policy=strict,
    )

    assert pending.lifecycle.next_entry_allowed is False
    assert complete.all_in_pnl is not None
    assert complete.all_in_pnl.all_in_net_pnl_usdt == Decimal("2.7782")
    assert complete.lifecycle.status is BybitDemoLifecycleStatus.FULLY_RECONCILED
    assert complete.lifecycle.next_entry_allowed is True
    assert complete.lifecycle.fully_reconciled_net_pnl is True


def test_open_trade_does_not_query_closed_pnl() -> None:
    reader = _Reader((_closed_pnl(),))

    result = reconcile_bybit_demo_post_trade_accounting(
        _trade(terminal=False),
        client=reader,
    )

    assert reader.calls == 0
    assert result.closed_pnl_read_attempted is False
    assert result.account_pnl is None
    assert result.lifecycle.status is BybitDemoLifecycleStatus.TRADE_NOT_TERMINAL
    assert result.lifecycle.next_entry_allowed is False


def test_post_trade_accounting_rejects_reader_with_order_writes() -> None:
    reader = _Reader((_closed_pnl(),))
    reader.order_writes_supported = True
    with pytest.raises(ValueError, match="must not support order writes"):
        reconcile_bybit_demo_post_trade_accounting(_trade(), client=reader)
