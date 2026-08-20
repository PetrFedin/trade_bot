from dataclasses import replace
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
from app.execution.bybit_demo_post_trade_accounting import (
    BybitDemoPostTradeAccountingResult,
)
from app.execution.bybit_demo_session_risk_ledger import (
    apply_fully_reconciled_trade_to_session_ledger,
    observe_bybit_demo_session_equity,
    start_bybit_demo_session_risk_ledger,
)
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
)
from app.strategy.crypto_session_risk import evaluate_crypto_session_risk


def _snapshot(
    *,
    order_link_id: str,
    pnl: str,
    fees: str,
    created: int,
    updated: int,
) -> BybitDemoPostTradeAccountingResult:
    pnl_value = Decimal(pnl)
    fee_value = Decimal(fees)
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
        entry_fees_usdt=fee_value / Decimal("2"),
        exit_fees_usdt=fee_value / Decimal("2"),
        execution_fees_usdt=fee_value,
        realized_gross_pnl_usdt=pnl_value + fee_value,
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
        open_fee_usdt=fee_value / Decimal("2"),
        close_fee_usdt=fee_value / Decimal("2"),
        created_time_ms=created,
        updated_time_ms=updated,
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


def test_session_ledger_makes_third_reconciled_loss_latch_before_next_entry() -> None:
    ledger = start_bybit_demo_session_risk_ledger(
        opening_equity_usdt=Decimal("1000")
    )
    for index, pnl in enumerate(("-10", "-5", "-3"), start=1):
        ledger = apply_fully_reconciled_trade_to_session_ledger(
            ledger,
            _snapshot(
                order_link_id=f"ASTRA-DEMO-E-LOSS{index}",
                pnl=pnl,
                fees="2",
                created=index * 100,
                updated=index * 100 + 50,
            ),
        )

    state = ledger.to_session_risk_state(current_equity_usdt=Decimal("982"))
    decision = evaluate_crypto_session_risk(state)

    assert state.realized_pnl_usdt == Decimal("-18")
    assert state.execution_cost_usdt == Decimal("6")
    assert state.consecutive_losses == 3
    assert state.peak_equity_usdt == Decimal("1000")
    assert decision.new_entries_allowed is False
    assert "SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED" in decision.reasons


def test_session_ledger_is_idempotent_and_positive_trade_resets_loss_streak() -> None:
    first = _snapshot(
        order_link_id="ASTRA-DEMO-E-ONE",
        pnl="-4",
        fees="1",
        created=100,
        updated=150,
    )
    profit = _snapshot(
        order_link_id="ASTRA-DEMO-E-TWO",
        pnl="10",
        fees="1",
        created=200,
        updated=250,
    )
    ledger = start_bybit_demo_session_risk_ledger(
        opening_equity_usdt=Decimal("1000")
    )
    ledger = apply_fully_reconciled_trade_to_session_ledger(ledger, first)
    same = apply_fully_reconciled_trade_to_session_ledger(ledger, first)
    assert same == ledger

    ledger = apply_fully_reconciled_trade_to_session_ledger(ledger, profit)
    state = ledger.to_session_risk_state(current_equity_usdt=Decimal("1006"))
    assert state.realized_pnl_usdt == Decimal("6")
    assert state.execution_cost_usdt == Decimal("2")
    assert state.consecutive_losses == 0
    assert state.peak_equity_usdt == Decimal("1006")
    assert ledger.peak_equity_usdt == Decimal("1006")


def test_session_ledger_sorts_out_of_order_reconciliation_by_close_time() -> None:
    late_loss = _snapshot(
        order_link_id="ASTRA-DEMO-E-LATE",
        pnl="-2",
        fees="1",
        created=300,
        updated=350,
    )
    early_profit = _snapshot(
        order_link_id="ASTRA-DEMO-E-EARLY",
        pnl="3",
        fees="1",
        created=100,
        updated=150,
    )
    ledger = start_bybit_demo_session_risk_ledger(
        opening_equity_usdt=Decimal("1000")
    )
    ledger = apply_fully_reconciled_trade_to_session_ledger(ledger, late_loss)
    ledger = apply_fully_reconciled_trade_to_session_ledger(ledger, early_profit)

    assert [item.entry_order_link_id for item in ledger.outcomes] == [
        "ASTRA-DEMO-E-EARLY",
        "ASTRA-DEMO-E-LATE",
    ]
    state = ledger.to_session_risk_state(current_equity_usdt=Decimal("1001"))
    assert state.consecutive_losses == 1
    assert state.realized_pnl_usdt == Decimal("1")
    assert state.peak_equity_usdt == Decimal("1003")
    assert ledger.peak_equity_usdt == Decimal("1003")


def test_wallet_observation_preserves_unrealized_high_water_for_later_drawdown() -> None:
    ledger = start_bybit_demo_session_risk_ledger(
        opening_equity_usdt=Decimal("1000")
    )
    ledger = observe_bybit_demo_session_equity(
        ledger,
        current_equity_usdt=Decimal("1100"),
    )

    state = ledger.to_session_risk_state(current_equity_usdt=Decimal("1000"))
    decision = evaluate_crypto_session_risk(state)

    assert ledger.peak_equity_usdt == Decimal("1100")
    assert state.peak_equity_usdt == Decimal("1100")
    assert state.current_equity_usdt == Decimal("1000")
    assert decision.flatten_required is True
    assert "SESSION_DRAWDOWN_LIMIT_BREACHED" in decision.reasons


def test_session_ledger_rejects_pending_or_conflicting_trade_evidence() -> None:
    snapshot = _snapshot(
        order_link_id="ASTRA-DEMO-E-CONFLICT",
        pnl="1",
        fees="1",
        created=100,
        updated=150,
    )
    ledger = start_bybit_demo_session_risk_ledger(
        opening_equity_usdt=Decimal("1000")
    )
    ledger = apply_fully_reconciled_trade_to_session_ledger(ledger, snapshot)

    conflicting = _snapshot(
        order_link_id="ASTRA-DEMO-E-CONFLICT",
        pnl="2",
        fees="1",
        created=100,
        updated=150,
    )
    with pytest.raises(ValueError, match="conflicting economics"):
        apply_fully_reconciled_trade_to_session_ledger(ledger, conflicting)

    pending = replace(snapshot, all_in_pnl=None)
    with pytest.raises(ValueError, match="fully reconciled"):
        apply_fully_reconciled_trade_to_session_ledger(
            start_bybit_demo_session_risk_ledger(
                opening_equity_usdt=Decimal("1000")
            ),
            pending,
        )
