from __future__ import annotations

import os
from decimal import Decimal

import pytest

from app.execution.bybit_demo_account_pnl_reconciliation import (
    BybitDemoAccountPnlReconciliation,
    BybitDemoAccountPnlStatus,
    BybitDemoClosedPnlRecord,
)
from app.execution.bybit_demo_funding_reconciliation import (
    BybitDemoAllInPnlReconciliation,
    BybitDemoFundingStatus,
)
from app.execution.bybit_demo_lifecycle_gate import (
    BybitDemoLifecycleDecision,
    BybitDemoLifecycleStatus,
)
from app.execution.bybit_demo_post_trade_accounting import BybitDemoPostTradeAccountingResult
from app.execution.bybit_demo_postgres_bootstrap import apply_bybit_demo_postgres_bootstrap
from app.execution.bybit_demo_postgres_session_risk_store import (
    PostgresBybitDemoSessionRiskLedgerStore,
)
from app.execution.bybit_demo_session_risk_ledger import start_bybit_demo_session_risk_ledger
from app.execution.bybit_demo_session_risk_runtime import (
    PostgresBybitDemoSessionRiskCommitter,
)
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
)

_DSN = os.environ.get("ASTRA_DEMO_RUNTIME_SESSION_RISK_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_DEMO_RUNTIME_SESSION_RISK_TEST_DSN is not configured",
)


def _snapshot(*, order_link_id: str, pnl: str) -> BybitDemoPostTradeAccountingResult:
    pnl_value = Decimal(pnl)
    fees = Decimal("2")
    trade = BybitDemoTradeMonitorResult(
        status=BybitDemoTradeMonitorStatus.CLOSED_RECONCILED,
        symbol="BTCUSDT",
        entry_order_link_id=order_link_id,
        entry_side="Buy",
        entry_quantity=Decimal("1"),
        exit_quantity=Decimal("1"),
        remaining_quantity=Decimal("0"),
        average_entry_price=Decimal("100"),
        average_exit_price=Decimal("101"),
        entry_fees_usdt=Decimal("1"),
        exit_fees_usdt=Decimal("1"),
        execution_fees_usdt=fees,
        realized_gross_pnl_usdt=pnl_value + fees,
        realized_net_pnl_after_execution_fees_usdt=pnl_value,
        reasons=(),
        terminal=True,
        next_entry_allowed=True,
    )
    record = BybitDemoClosedPnlRecord(
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("1"),
        average_entry_price=Decimal("100"),
        average_exit_price=Decimal("101"),
        closed_pnl_usdt=pnl_value,
        open_fee_usdt=Decimal("1"),
        close_fee_usdt=Decimal("1"),
        created_time_ms=100,
        updated_time_ms=150,
    )
    account = BybitDemoAccountPnlReconciliation(
        status=BybitDemoAccountPnlStatus.CLOSED_PNL_RECONCILED,
        symbol="BTCUSDT",
        matched_record=record,
        fill_net_after_execution_fees_usdt=pnl_value,
        account_closed_pnl_usdt=pnl_value,
        account_minus_fill_net_usdt=Decimal("0"),
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
        account_closed_pnl_usdt=pnl_value,
        funding_net_usdt=Decimal("0"),
        all_in_net_pnl_usdt=pnl_value,
        funding_entry_count=0,
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


def test_terminal_committer_survives_restart_is_idempotent_and_rejects_conflict() -> None:
    applied = apply_bybit_demo_postgres_bootstrap(
        _DSN,
        confirmation_phrase="APPLY_BYBIT_DEMO_V119_V122",
    )
    assert applied.passed is True

    store = PostgresBybitDemoSessionRiskLedgerStore(_DSN)
    initial = store.initialize(
        start_bybit_demo_session_risk_ledger(opening_equity_usdt=Decimal("1000"))
    )
    assert initial.ledger.outcomes == ()

    snapshot = _snapshot(order_link_id="ASTRA-DEMO-E-RUNTIME-RISK", pnl="-5")
    first = PostgresBybitDemoSessionRiskCommitter(store).commit(snapshot)
    assert first.idempotent_existing_outcome is False
    assert first.outcome_count == 1
    assert first.entry_order_link_id == "ASTRA-DEMO-E-RUNTIME-RISK"

    restarted_store = PostgresBybitDemoSessionRiskLedgerStore(_DSN)
    restarted = restarted_store.load_active()
    assert restarted.revision == first.ledger_revision_sha256
    assert restarted.ledger.cumulative_realized_all_in_pnl_usdt == Decimal("-5")
    assert restarted.ledger.to_session_risk_state(
        current_equity_usdt=Decimal("995")
    ).consecutive_losses == 1

    repeated = PostgresBybitDemoSessionRiskCommitter(restarted_store).commit(snapshot)
    assert repeated.idempotent_existing_outcome is True
    assert repeated.ledger_revision_sha256 == first.ledger_revision_sha256
    assert repeated.outcome_count == 1

    conflicting = _snapshot(order_link_id="ASTRA-DEMO-E-RUNTIME-RISK", pnl="7")
    with pytest.raises(ValueError, match="conflicting economics"):
        PostgresBybitDemoSessionRiskCommitter(restarted_store).commit(conflicting)

    final = restarted_store.load_active()
    assert final.revision == first.ledger_revision_sha256
    assert final.ledger.cumulative_realized_all_in_pnl_usdt == Decimal("-5")
    assert len(final.ledger.outcomes) == 1
