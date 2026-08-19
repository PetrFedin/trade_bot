from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.bybit_demo_account_pnl_reconciliation import (
    BybitDemoAccountPnlReconciliation,
    BybitDemoAccountPnlStatus,
)
from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionFinal
from app.execution.bybit_demo_funding_reconciliation import (
    BybitDemoAllInPnlReconciliation,
    BybitDemoFundingStatus,
)
from app.execution.bybit_demo_lifecycle_gate import (
    BybitDemoLifecycleDecision,
    BybitDemoLifecycleStatus,
)
from app.execution.bybit_demo_post_trade_accounting import BybitDemoPostTradeAccountingResult
from app.execution.bybit_demo_profit_preservation_evidence import (
    build_bybit_demo_profit_preservation_evidence,
    summarize_bybit_demo_profit_preservation_evidence,
)
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
)
from app.strategy.crypto_perp import CryptoSide


def _excursion(*, exit_r: str = "0.4", peak_r: str = "2") -> BybitDemoTradeExcursionFinal:
    peak = Decimal(peak_r)
    exit_value = Decimal(exit_r)
    return BybitDemoTradeExcursionFinal(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        observation_count=5,
        observed_peak_favorable_r=peak,
        observed_max_adverse_r=Decimal("0.5"),
        realized_gross_exit_r=exit_value,
        observed_peak_capture_fraction=None if peak <= 0 else exit_value / peak,
        giveback_from_observed_peak_to_exit_r=max(peak - exit_value, Decimal("0")),
        exit_exceeded_observed_peak=exit_value > peak,
        positive_observed_peak_nonpositive_exit=peak > 0 and exit_value <= 0,
        partial_close_seen=False,
    )


def _accounting(
    *,
    gross: str,
    fill: str,
    account: str,
    funding: str | None,
) -> BybitDemoPostTradeAccountingResult:
    gross_value = Decimal(gross)
    fill_value = Decimal(fill)
    account_value = Decimal(account)
    fees = gross_value - fill_value
    trade = BybitDemoTradeMonitorResult(
        status=BybitDemoTradeMonitorStatus.CLOSED_RECONCILED,
        symbol="BTCUSDT",
        entry_order_link_id="ASTRA-DEMO-E-PROFIT-EVIDENCE",
        entry_side="Buy",
        entry_quantity=Decimal("2"),
        exit_quantity=Decimal("2"),
        remaining_quantity=Decimal("0"),
        average_entry_price=Decimal("100"),
        average_exit_price=Decimal("102") if gross_value > 0 else Decimal("99"),
        entry_fees_usdt=fees / Decimal("2"),
        exit_fees_usdt=fees / Decimal("2"),
        execution_fees_usdt=fees,
        realized_gross_pnl_usdt=gross_value,
        realized_net_pnl_after_execution_fees_usdt=fill_value,
        reasons=(),
        terminal=True,
        next_entry_allowed=True,
    )
    funding_reconciled = funding is not None
    account_result = BybitDemoAccountPnlReconciliation(
        status=BybitDemoAccountPnlStatus.CLOSED_PNL_RECONCILED,
        symbol="BTCUSDT",
        matched_record=None,
        fill_net_after_execution_fees_usdt=fill_value,
        account_closed_pnl_usdt=account_value,
        account_minus_fill_net_usdt=account_value - fill_value,
        execution_fee_difference_usdt=Decimal("0"),
        reasons=(),
        account_closed_pnl_reconciled=True,
        funding_reconciled=funding_reconciled,
        fully_reconciled_net_pnl=funding_reconciled,
        next_entry_allowed=funding_reconciled,
    )
    all_in = None
    if funding is not None:
        funding_value = Decimal(funding)
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
        status=(
            BybitDemoLifecycleStatus.FULLY_RECONCILED
            if funding_reconciled
            else BybitDemoLifecycleStatus.ACCOUNT_PNL_RECONCILED_FUNDING_PENDING
        ),
        reasons=() if funding_reconciled else ("FUNDING_RECONCILIATION_PENDING",),
        next_entry_allowed=funding_reconciled,
        trade_terminal=True,
        account_closed_pnl_reconciled=True,
        funding_reconciled=funding_reconciled,
        fully_reconciled_net_pnl=funding_reconciled,
    )
    return BybitDemoPostTradeAccountingResult(
        trade=trade,
        account_pnl=account_result,
        all_in_pnl=all_in,
        lifecycle=lifecycle,
        closed_pnl_read_attempted=True,
        funding_ledger_supplied=funding_reconciled,
    )


def test_evidence_joins_peak_exit_fees_account_and_funding() -> None:
    evidence = build_bybit_demo_profit_preservation_evidence(
        _excursion(exit_r="0.4", peak_r="2"),
        _accounting(gross="4", fill="3", account="2.5", funding="-1"),
    )

    assert evidence.observed_peak_favorable_r == Decimal("2")
    assert evidence.realized_gross_exit_r == Decimal("0.4")
    assert evidence.observed_peak_capture_fraction == Decimal("0.2")
    assert evidence.giveback_from_observed_peak_to_exit_r == Decimal("1.6")
    assert evidence.realized_gross_pnl_usdt == Decimal("4")
    assert evidence.realized_net_after_execution_fees_usdt == Decimal("3")
    assert evidence.execution_fees_usdt == Decimal("1")
    assert evidence.account_closed_pnl_usdt == Decimal("2.5")
    assert evidence.funding_net_usdt == Decimal("-1")
    assert evidence.all_in_net_pnl_usdt == Decimal("1.5")
    assert evidence.fully_reconciled_all_in is True
    assert evidence.positive_peak_nonpositive_all_in is False


def test_evidence_separates_exit_fee_account_and_funding_profit_flips() -> None:
    exit_flip = build_bybit_demo_profit_preservation_evidence(
        _excursion(exit_r="-0.2"),
        _accounting(gross="-2", fill="-2.5", account="-2.5", funding="0"),
    )
    fee_flip = build_bybit_demo_profit_preservation_evidence(
        _excursion(exit_r="0.1"),
        _accounting(gross="1", fill="-0.1", account="-0.1", funding="0"),
    )
    account_flip = build_bybit_demo_profit_preservation_evidence(
        _excursion(exit_r="0.2"),
        _accounting(gross="2", fill="1.5", account="-0.1", funding="0"),
    )
    funding_flip = build_bybit_demo_profit_preservation_evidence(
        _excursion(exit_r="0.3"),
        _accounting(gross="3", fill="2", account="2", funding="-3"),
    )

    assert exit_flip.positive_peak_nonpositive_gross_exit is True
    assert fee_flip.gross_positive_fill_nonpositive is True
    assert account_flip.fill_positive_account_nonpositive is True
    assert funding_flip.account_positive_all_in_nonpositive is True
    assert exit_flip.positive_peak_nonpositive_all_in is True
    assert fee_flip.positive_peak_nonpositive_all_in is True
    assert account_flip.positive_peak_nonpositive_all_in is True
    assert funding_flip.positive_peak_nonpositive_all_in is True

    summary = summarize_bybit_demo_profit_preservation_evidence(
        (exit_flip, fee_flip, account_flip, funding_flip)
    )
    assert summary["positive_peak_nonpositive_gross_exit_count"] == 1
    assert summary["gross_positive_fill_nonpositive_count"] == 1
    assert summary["fill_positive_account_nonpositive_count"] == 1
    assert summary["account_positive_all_in_nonpositive_count"] == 1
    assert summary["positive_peak_nonpositive_all_in_count"] == 4
    assert summary["fully_reconciled_all_in_count"] == 4
    assert summary["diagnostics_only"] is True
    assert summary["exit_threshold_retuning_allowed"] is False
    assert summary["strategy_promotion_allowed"] is False


def test_pending_funding_does_not_claim_final_profit_or_loss() -> None:
    evidence = build_bybit_demo_profit_preservation_evidence(
        _excursion(exit_r="0.4"),
        _accounting(gross="4", fill="3", account="2.5", funding=None),
    )

    assert evidence.fully_reconciled_all_in is False
    assert evidence.all_in_net_pnl_usdt is None
    assert evidence.positive_peak_nonpositive_all_in is None
    assert evidence.profit_outcome_status.value == "ALL_IN_ACCOUNTING_PENDING"
    summary = summarize_bybit_demo_profit_preservation_evidence((evidence,))
    assert summary["fully_reconciled_all_in_count"] == 0
    assert summary["accounting_pending_count"] == 1


def test_evidence_rejects_conflicting_excursion_and_realized_gross_sign() -> None:
    with pytest.raises(ValueError, match="positive gross exit R conflicts"):
        build_bybit_demo_profit_preservation_evidence(
            _excursion(exit_r="0.2"),
            _accounting(gross="-1", fill="-1.2", account="-1.2", funding="0"),
        )
