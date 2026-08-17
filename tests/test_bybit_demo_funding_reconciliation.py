from decimal import Decimal

from app.execution.bybit_demo_account_pnl_reconciliation import (
    BybitDemoAccountPnlReconciliation,
    BybitDemoAccountPnlStatus,
    BybitDemoClosedPnlRecord,
)
from app.execution.bybit_demo_funding_reconciliation import (
    BybitDemoFundingLedgerEntry,
    BybitDemoFundingLedgerWindow,
    BybitDemoFundingStatus,
    apply_funding_to_account_view,
    reconcile_bybit_demo_funding,
)


def _account() -> BybitDemoAccountPnlReconciliation:
    record = BybitDemoClosedPnlRecord(
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("1"),
        average_entry_price=Decimal("100"),
        average_exit_price=Decimal("103"),
        closed_pnl_usdt=Decimal("2.8782"),
        open_fee_usdt=Decimal("0.06"),
        close_fee_usdt=Decimal("0.0618"),
        created_time_ms=1000,
        updated_time_ms=5000,
    )
    return BybitDemoAccountPnlReconciliation(
        status=BybitDemoAccountPnlStatus.CLOSED_PNL_RECONCILED,
        symbol="BTCUSDT",
        matched_record=record,
        fill_net_after_execution_fees_usdt=Decimal("2.8782"),
        account_closed_pnl_usdt=Decimal("2.8782"),
        account_minus_fill_net_usdt=Decimal("0"),
        execution_fee_difference_usdt=Decimal("0"),
        reasons=(),
        account_closed_pnl_reconciled=True,
        funding_reconciled=False,
        fully_reconciled_net_pnl=False,
        next_entry_allowed=True,
    )


def test_full_ledger_coverage_reconciles_all_in_pnl() -> None:
    ledger = BybitDemoFundingLedgerWindow(
        coverage_start_ms=0,
        coverage_end_ms=6000,
        entries=(
            BybitDemoFundingLedgerEntry(
                symbol="BTCUSDT",
                transaction_time_ms=2000,
                amount_usdt=Decimal("-0.20"),
                reference_id="funding-1",
            ),
            BybitDemoFundingLedgerEntry(
                symbol="BTCUSDT",
                transaction_time_ms=4000,
                amount_usdt=Decimal("0.05"),
                reference_id="funding-2",
            ),
        ),
    )

    funding = reconcile_bybit_demo_funding(_account(), ledger)
    account_view = apply_funding_to_account_view(_account(), funding)

    assert funding.status is BybitDemoFundingStatus.FUNDING_RECONCILED
    assert funding.funding_net_usdt == Decimal("-0.15")
    assert funding.all_in_net_pnl_usdt == Decimal("2.7282")
    assert funding.funding_entry_count == 2
    assert funding.fully_reconciled_net_pnl is True
    assert account_view.funding_reconciled is True
    assert account_view.fully_reconciled_net_pnl is True
    assert funding.live_mainnet_order_routing_allowed is False


def test_incomplete_ledger_coverage_never_assumes_zero_funding() -> None:
    ledger = BybitDemoFundingLedgerWindow(
        coverage_start_ms=2000,
        coverage_end_ms=6000,
        entries=(),
    )

    funding = reconcile_bybit_demo_funding(_account(), ledger)

    assert funding.status is BybitDemoFundingStatus.LEDGER_COVERAGE_INCOMPLETE
    assert funding.funding_net_usdt is None
    assert funding.all_in_net_pnl_usdt is None
    assert funding.funding_reconciled is False
    assert funding.fully_reconciled_net_pnl is False


def test_duplicate_funding_reference_is_counted_once() -> None:
    entry = BybitDemoFundingLedgerEntry(
        symbol="BTCUSDT",
        transaction_time_ms=2000,
        amount_usdt=Decimal("-0.20"),
        reference_id="funding-1",
    )
    ledger = BybitDemoFundingLedgerWindow(
        coverage_start_ms=0,
        coverage_end_ms=6000,
        entries=(entry, entry),
    )

    funding = reconcile_bybit_demo_funding(_account(), ledger)

    assert funding.funding_net_usdt == Decimal("-0.20")
    assert funding.funding_entry_count == 1


def test_unreconciled_account_pnl_blocks_funding_stage() -> None:
    account = _account()
    account = BybitDemoAccountPnlReconciliation(
        **{
            **account.__dict__,
            "status": BybitDemoAccountPnlStatus.CLOSED_PNL_MISMATCH,
            "account_closed_pnl_reconciled": False,
        }
    )
    ledger = BybitDemoFundingLedgerWindow(
        coverage_start_ms=0,
        coverage_end_ms=6000,
        entries=(),
    )

    funding = reconcile_bybit_demo_funding(account, ledger)

    assert funding.status is BybitDemoFundingStatus.ACCOUNT_PNL_NOT_RECONCILED
    assert funding.funding_reconciled is False
    assert funding.fully_reconciled_net_pnl is False
