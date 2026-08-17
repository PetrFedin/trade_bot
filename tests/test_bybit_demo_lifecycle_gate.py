from decimal import Decimal

from app.execution.bybit_demo_account_pnl_reconciliation import (
    BybitDemoAccountPnlReconciliation,
    BybitDemoAccountPnlStatus,
)
from app.execution.bybit_demo_lifecycle_gate import (
    BybitDemoLifecyclePolicy,
    BybitDemoLifecycleStatus,
    evaluate_bybit_demo_lifecycle,
)
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
)


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


def _account(*, reconciled: bool = True, funding: bool = False) -> BybitDemoAccountPnlReconciliation:
    return BybitDemoAccountPnlReconciliation(
        status=(
            BybitDemoAccountPnlStatus.CLOSED_PNL_RECONCILED
            if reconciled
            else BybitDemoAccountPnlStatus.CLOSED_PNL_MISMATCH
        ),
        symbol="BTCUSDT",
        matched_record=None,
        fill_net_after_execution_fees_usdt=Decimal("2.8782"),
        account_closed_pnl_usdt=Decimal("2.8782") if reconciled else Decimal("2.5"),
        account_minus_fill_net_usdt=Decimal("0") if reconciled else Decimal("-0.3782"),
        execution_fee_difference_usdt=Decimal("0"),
        reasons=() if reconciled else ("ACCOUNT_CLOSED_PNL_DIFFERS_FROM_FILL_NET",),
        account_closed_pnl_reconciled=reconciled,
        funding_reconciled=funding,
        fully_reconciled_net_pnl=reconciled and funding,
        next_entry_allowed=True,
    )


def test_default_gate_blocks_reuse_until_account_closed_pnl_is_reconciled() -> None:
    decision = evaluate_bybit_demo_lifecycle(_trade(), None)

    assert decision.status is BybitDemoLifecycleStatus.ACCOUNT_PNL_PENDING
    assert decision.next_entry_allowed is False
    assert decision.trade_terminal is True
    assert decision.account_closed_pnl_reconciled is False
    assert decision.fully_reconciled_net_pnl is False


def test_account_mismatch_keeps_symbol_locked_by_default() -> None:
    decision = evaluate_bybit_demo_lifecycle(_trade(), _account(reconciled=False))

    assert decision.status is BybitDemoLifecycleStatus.ACCOUNT_PNL_MISMATCH
    assert decision.next_entry_allowed is False
    assert "ACCOUNT_CLOSED_PNL_NOT_RECONCILED" in decision.reasons
    assert decision.live_mainnet_order_routing_allowed is False


def test_default_gate_keeps_reconciled_closed_pnl_locked_until_funding() -> None:
    decision = evaluate_bybit_demo_lifecycle(_trade(), _account())

    assert decision.status is (
        BybitDemoLifecycleStatus.ACCOUNT_PNL_RECONCILED_FUNDING_PENDING
    )
    assert decision.next_entry_allowed is False
    assert decision.account_closed_pnl_reconciled is True
    assert decision.funding_reconciled is False
    assert decision.fully_reconciled_net_pnl is False


def test_explicit_relaxed_funding_policy_can_unlock_before_full_accounting() -> None:
    policy = BybitDemoLifecyclePolicy(require_funding_before_next_entry=False)
    decision = evaluate_bybit_demo_lifecycle(_trade(), _account(), policy=policy)

    assert decision.status is (
        BybitDemoLifecycleStatus.ACCOUNT_PNL_RECONCILED_FUNDING_PENDING
    )
    assert decision.next_entry_allowed is True
    assert decision.fully_reconciled_net_pnl is False


def test_default_funding_policy_unlocks_only_after_full_accounting() -> None:
    pending = evaluate_bybit_demo_lifecycle(_trade(), _account())
    complete = evaluate_bybit_demo_lifecycle(
        _trade(),
        _account(funding=True),
    )

    assert pending.next_entry_allowed is False
    assert complete.status is BybitDemoLifecycleStatus.FULLY_RECONCILED
    assert complete.next_entry_allowed is True
    assert complete.fully_reconciled_net_pnl is True


def test_open_trade_is_never_unlocked_by_account_record() -> None:
    decision = evaluate_bybit_demo_lifecycle(_trade(terminal=False), _account())

    assert decision.status is BybitDemoLifecycleStatus.TRADE_NOT_TERMINAL
    assert decision.next_entry_allowed is False
    assert decision.trade_terminal is False
