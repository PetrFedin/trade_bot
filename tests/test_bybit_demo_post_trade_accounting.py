from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_funding_reconciliation import (
    BybitDemoFundingLedgerEntry,
    BybitDemoFundingLedgerWindow,
)
from app.execution.bybit_demo_lifecycle_gate import (
    BybitDemoLifecyclePolicy,
    BybitDemoLifecycleStatus,
)
from app.execution.bybit_demo_post_trade_accounting import (
    BybitDemoProfitOutcomeStatus,
    reconcile_bybit_demo_post_trade_accounting,
    reconcile_bybit_demo_trade_lifecycle,
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


class _FundingReader(_Reader):
    def __init__(
        self,
        rows: tuple[object, ...],
        *,
        transaction_rows: tuple[object, ...] = (),
        transaction_error: Exception | None = None,
    ) -> None:
        super().__init__(rows)
        self.transaction_rows = transaction_rows
        self.transaction_error = transaction_error
        self.transaction_calls = 0

    def get_transaction_log(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 50,
        max_pages: int = 20,
        transaction_type: str | None = None,
    ) -> tuple[object, ...]:
        assert symbol == "BTCUSDT"
        assert start_time_ms == 1000
        assert end_time_ms == 5000
        assert limit == 50
        assert max_pages == 20
        assert transaction_type == "SETTLEMENT"
        self.transaction_calls += 1
        if self.transaction_error is not None:
            raise self.transaction_error
        return self.transaction_rows


class _TradeReader:
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        *,
        entry_rows: tuple[Mapping[str, Any], ...],
        all_rows: tuple[Mapping[str, Any], ...],
        positions: tuple[BybitDemoPosition, ...] = (),
    ) -> None:
        self.entry_rows = entry_rows
        self.all_rows = all_rows
        self.positions = positions
        self.execution_calls = 0
        self.position_calls = 0

    def get_executions(
        self,
        *,
        symbol: str,
        order_link_id: str | None = None,
        limit: int = 50,
    ) -> tuple[Mapping[str, Any], ...]:
        assert symbol == "BTCUSDT"
        assert limit == 100
        self.execution_calls += 1
        return self.entry_rows if order_link_id is not None else self.all_rows

    def get_positions(
        self,
        *,
        settle_coin: str = "USDT",
    ) -> tuple[BybitDemoPosition, ...]:
        assert settle_coin == "USDT"
        self.position_calls += 1
        return self.positions


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


def _funding_settlement(amount: str = "-0.10") -> dict[str, str]:
    return {
        "id": "funding-1",
        "symbol": "BTCUSDT",
        "category": "linear",
        "currency": "USDT",
        "transactionTime": "3000",
        "type": "SETTLEMENT",
        "funding": amount,
        "cashFlow": "0",
        "fee": "0",
    }


def _execution(
    *,
    exec_id: str,
    side: str,
    quantity: str,
    price: str,
    fee: str,
    exec_time: str,
    order_link_id: str,
) -> dict[str, str]:
    return {
        "symbol": "BTCUSDT",
        "execId": exec_id,
        "orderLinkId": order_link_id,
        "side": side,
        "execQty": quantity,
        "execPrice": price,
        "execFee": fee,
        "execTime": exec_time,
    }


def test_unified_snapshot_reconciles_terminal_trade_to_all_in_pnl() -> None:
    entry = _execution(
        exec_id="entry-1",
        side="Buy",
        quantity="1",
        price="100",
        fee="0.06",
        exec_time="1000",
        order_link_id="ASTRA-DEMO-E-ABC123",
    )
    exit_fill = _execution(
        exec_id="exit-1",
        side="Sell",
        quantity="1",
        price="103",
        fee="0.0618",
        exec_time="5000",
        order_link_id="BYBIT-PROTECTION",
    )
    trade_reader = _TradeReader(
        entry_rows=(entry,),
        all_rows=(entry, exit_fill),
    )
    accounting_reader = _FundingReader(
        (_closed_pnl(),),
        transaction_rows=(_funding_settlement(),),
    )

    result = reconcile_bybit_demo_trade_lifecycle(
        trade_client=trade_reader,
        accounting_client=accounting_reader,
        symbol="BTCUSDT",
        entry_side="Buy",
        entry_order_link_id="ASTRA-DEMO-E-ABC123",
    )

    assert trade_reader.execution_calls == 2
    assert trade_reader.position_calls == 1
    assert result.trade.status is BybitDemoTradeMonitorStatus.CLOSED_RECONCILED
    assert result.trade.terminal is True
    assert result.account_pnl is not None
    assert result.account_pnl.account_closed_pnl_reconciled is True
    assert result.all_in_pnl is not None
    assert result.all_in_pnl.all_in_net_pnl_usdt == Decimal("2.7782")
    assert result.fully_reconciled_all_in_net_pnl_usdt == Decimal("2.7782")
    assert result.profit_outcome_status is (
        BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_PROFIT
    )
    assert result.closed_in_profit_after_all_reconciled_costs is True
    assert result.lifecycle.status is BybitDemoLifecycleStatus.FULLY_RECONCILED
    assert result.lifecycle.next_entry_allowed is True
    assert result.live_mainnet_order_routing_allowed is False


def test_unified_snapshot_keeps_open_trade_locked_without_account_reads() -> None:
    entry = _execution(
        exec_id="entry-1",
        side="Buy",
        quantity="1",
        price="100",
        fee="0.06",
        exec_time="1000",
        order_link_id="ASTRA-DEMO-E-ABC123",
    )
    position = BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal("1"),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("80"),
    )
    trade_reader = _TradeReader(
        entry_rows=(entry,),
        all_rows=(entry,),
        positions=(position,),
    )
    accounting_reader = _FundingReader((_closed_pnl(),))

    result = reconcile_bybit_demo_trade_lifecycle(
        trade_client=trade_reader,
        accounting_client=accounting_reader,
        symbol="BTCUSDT",
        entry_side="Buy",
        entry_order_link_id="ASTRA-DEMO-E-ABC123",
    )

    assert result.trade.status is BybitDemoTradeMonitorStatus.OPEN
    assert result.trade.terminal is False
    assert accounting_reader.calls == 0
    assert accounting_reader.transaction_calls == 0
    assert result.account_pnl is None
    assert result.all_in_pnl is None
    assert result.profit_outcome_status is BybitDemoProfitOutcomeStatus.TRADE_OPEN
    assert result.closed_in_profit_after_all_reconciled_costs is None
    assert result.lifecycle.status is BybitDemoLifecycleStatus.TRADE_NOT_TERMINAL
    assert result.lifecycle.next_entry_allowed is False


def test_closed_pnl_without_funding_proof_stays_locked_by_default() -> None:
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
    assert result.funding_transaction_log_read_attempted is False
    assert result.profit_outcome_status is (
        BybitDemoProfitOutcomeStatus.ALL_IN_ACCOUNTING_PENDING
    )
    assert result.fully_reconciled_all_in_net_pnl_usdt is None
    assert result.closed_in_profit_after_all_reconciled_costs is None
    assert result.lifecycle.status is (
        BybitDemoLifecycleStatus.ACCOUNT_PNL_RECONCILED_FUNDING_PENDING
    )
    assert result.lifecycle.next_entry_allowed is False
    assert result.lifecycle.fully_reconciled_net_pnl is False
    assert result.live_mainnet_order_routing_allowed is False


def test_transaction_log_automatically_reconciles_all_in_pnl() -> None:
    reader = _FundingReader(
        (_closed_pnl(),),
        transaction_rows=(_funding_settlement(),),
    )

    result = reconcile_bybit_demo_post_trade_accounting(
        _trade(),
        client=reader,
    )

    assert reader.transaction_calls == 1
    assert result.funding_transaction_log_read_attempted is True
    assert result.funding_transaction_log_row_count == 1
    assert result.funding_transaction_log_error_type is None
    assert result.funding_ledger_source == "BYBIT_TRANSACTION_LOG"
    assert result.all_in_pnl is not None
    assert result.all_in_pnl.funding_net_usdt == Decimal("-0.10")
    assert result.all_in_pnl.all_in_net_pnl_usdt == Decimal("2.7782")
    assert result.profit_outcome_status is (
        BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_PROFIT
    )
    assert result.closed_in_profit_after_all_reconciled_costs is True
    assert result.lifecycle.status is BybitDemoLifecycleStatus.FULLY_RECONCILED
    assert result.lifecycle.fully_reconciled_net_pnl is True


def test_funding_can_turn_fill_profit_into_fully_reconciled_loss() -> None:
    reader = _FundingReader(
        (_closed_pnl(),),
        transaction_rows=(_funding_settlement("-3.00"),),
    )

    result = reconcile_bybit_demo_post_trade_accounting(
        _trade(),
        client=reader,
    )

    assert result.trade.realized_net_pnl_after_execution_fees_usdt == Decimal("2.8782")
    assert result.all_in_pnl is not None
    assert result.all_in_pnl.funding_net_usdt == Decimal("-3.00")
    assert result.fully_reconciled_all_in_net_pnl_usdt == Decimal("-0.1218")
    assert result.profit_outcome_status is (
        BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_LOSS
    )
    assert result.closed_in_profit_after_all_reconciled_costs is False
    assert result.lifecycle.status is BybitDemoLifecycleStatus.FULLY_RECONCILED


def test_complete_empty_transaction_log_proves_zero_funding() -> None:
    reader = _FundingReader((_closed_pnl(),), transaction_rows=())

    result = reconcile_bybit_demo_post_trade_accounting(
        _trade(),
        client=reader,
    )

    assert result.all_in_pnl is not None
    assert result.all_in_pnl.funding_entry_count == 0
    assert result.all_in_pnl.funding_net_usdt == Decimal("0")
    assert result.all_in_pnl.all_in_net_pnl_usdt == Decimal("2.8782")
    assert result.profit_outcome_status is (
        BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_PROFIT
    )
    assert result.lifecycle.status is BybitDemoLifecycleStatus.FULLY_RECONCILED
    assert result.lifecycle.next_entry_allowed is True


def test_transaction_log_api_failure_stays_pending_and_blocks_default_lifecycle() -> None:
    reader = _FundingReader(
        (_closed_pnl(),),
        transaction_error=RuntimeError("remote details are not surfaced"),
    )

    result = reconcile_bybit_demo_post_trade_accounting(
        _trade(),
        client=reader,
    )

    assert result.funding_transaction_log_read_attempted is True
    assert result.funding_transaction_log_error_type == "RuntimeError"
    assert result.funding_ledger_source is None
    assert result.all_in_pnl is None
    assert result.profit_outcome_status is (
        BybitDemoProfitOutcomeStatus.ALL_IN_ACCOUNTING_PENDING
    )
    assert result.closed_in_profit_after_all_reconciled_costs is None
    assert result.lifecycle.status is (
        BybitDemoLifecycleStatus.ACCOUNT_PNL_RECONCILED_FUNDING_PENDING
    )
    assert result.lifecycle.next_entry_allowed is False


def test_explicit_covered_funding_ledger_unlocks_default_lifecycle() -> None:
    reader = _FundingReader((_closed_pnl(),))
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
    )

    assert reader.transaction_calls == 0
    assert complete.funding_ledger_source == "SUPPLIED"
    assert complete.all_in_pnl is not None
    assert complete.all_in_pnl.all_in_net_pnl_usdt == Decimal("2.7782")
    assert complete.profit_outcome_status is (
        BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_PROFIT
    )
    assert complete.lifecycle.status is BybitDemoLifecycleStatus.FULLY_RECONCILED
    assert complete.lifecycle.next_entry_allowed is True
    assert complete.lifecycle.fully_reconciled_net_pnl is True


def test_relaxed_policy_can_unlock_when_funding_read_is_unavailable() -> None:
    reader = _FundingReader(
        (_closed_pnl(),),
        transaction_error=RuntimeError("blocked"),
    )
    relaxed = BybitDemoLifecyclePolicy(require_funding_before_next_entry=False)

    result = reconcile_bybit_demo_post_trade_accounting(
        _trade(),
        client=reader,
        lifecycle_policy=relaxed,
    )

    assert result.all_in_pnl is None
    assert result.profit_outcome_status is (
        BybitDemoProfitOutcomeStatus.ALL_IN_ACCOUNTING_PENDING
    )
    assert result.closed_in_profit_after_all_reconciled_costs is None
    assert result.lifecycle.status is (
        BybitDemoLifecycleStatus.ACCOUNT_PNL_RECONCILED_FUNDING_PENDING
    )
    assert result.lifecycle.next_entry_allowed is True
    assert result.lifecycle.fully_reconciled_net_pnl is False


def test_open_trade_does_not_query_closed_pnl() -> None:
    reader = _FundingReader((_closed_pnl(),))

    result = reconcile_bybit_demo_post_trade_accounting(
        _trade(terminal=False),
        client=reader,
    )

    assert reader.calls == 0
    assert reader.transaction_calls == 0
    assert result.closed_pnl_read_attempted is False
    assert result.account_pnl is None
    assert result.profit_outcome_status is BybitDemoProfitOutcomeStatus.TRADE_OPEN
    assert result.closed_in_profit_after_all_reconciled_costs is None
    assert result.lifecycle.status is BybitDemoLifecycleStatus.TRADE_NOT_TERMINAL
    assert result.lifecycle.next_entry_allowed is False


def test_post_trade_accounting_rejects_reader_with_order_writes() -> None:
    reader = _Reader((_closed_pnl(),))
    reader.order_writes_supported = True
    with pytest.raises(ValueError, match="must not support order writes"):
        reconcile_bybit_demo_post_trade_accounting(_trade(), client=reader)
