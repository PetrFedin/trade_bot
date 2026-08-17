from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from app.execution.bybit_demo_account_pnl_reconciliation import (
    BybitDemoAccountPnlReconciliation,
    BybitDemoClosedPnlRecord,
)

_ZERO = Decimal("0")


class BybitDemoFundingStatus(StrEnum):
    ACCOUNT_PNL_NOT_RECONCILED = "ACCOUNT_PNL_NOT_RECONCILED"
    LEDGER_COVERAGE_INCOMPLETE = "LEDGER_COVERAGE_INCOMPLETE"
    FUNDING_RECONCILED = "FUNDING_RECONCILED"


@dataclass(frozen=True)
class BybitDemoFundingLedgerEntry:
    symbol: str
    transaction_time_ms: int
    amount_usdt: Decimal
    reference_id: str


@dataclass(frozen=True)
class BybitDemoFundingLedgerWindow:
    coverage_start_ms: int
    coverage_end_ms: int
    entries: tuple[BybitDemoFundingLedgerEntry, ...]

    def validate(self) -> None:
        if self.coverage_start_ms < 0 or self.coverage_end_ms < 0:
            raise ValueError("funding ledger coverage timestamps cannot be negative")
        if self.coverage_end_ms < self.coverage_start_ms:
            raise ValueError("funding ledger coverage end must not precede start")
        for entry in self.entries:
            if entry.transaction_time_ms < self.coverage_start_ms:
                raise ValueError("funding entry precedes declared ledger coverage")
            if entry.transaction_time_ms > self.coverage_end_ms:
                raise ValueError("funding entry exceeds declared ledger coverage")
            if not entry.amount_usdt.is_finite():
                raise ValueError("funding amount must be finite")


@dataclass(frozen=True)
class BybitDemoAllInPnlReconciliation:
    status: BybitDemoFundingStatus
    symbol: str
    account_closed_pnl_usdt: Decimal | None
    funding_net_usdt: Decimal | None
    all_in_net_pnl_usdt: Decimal | None
    funding_entry_count: int
    reasons: tuple[str, ...]
    account_closed_pnl_reconciled: bool
    funding_reconciled: bool
    fully_reconciled_net_pnl: bool
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def reconcile_bybit_demo_funding(
    account_pnl: BybitDemoAccountPnlReconciliation,
    ledger: BybitDemoFundingLedgerWindow,
) -> BybitDemoAllInPnlReconciliation:
    """Add funding only when ledger coverage fully contains the closed trade interval."""

    ledger.validate()
    record = account_pnl.matched_record
    if (
        not account_pnl.account_closed_pnl_reconciled
        or account_pnl.account_closed_pnl_usdt is None
        or record is None
    ):
        return _result(
            BybitDemoFundingStatus.ACCOUNT_PNL_NOT_RECONCILED,
            account_pnl,
            funding_net=None,
            funding_count=0,
            reasons=("ACCOUNT_CLOSED_PNL_MUST_RECONCILE_BEFORE_FUNDING",),
        )

    if not _window_covers_trade(ledger, record):
        return _result(
            BybitDemoFundingStatus.LEDGER_COVERAGE_INCOMPLETE,
            account_pnl,
            funding_net=None,
            funding_count=0,
            reasons=("FUNDING_LEDGER_DOES_NOT_COVER_FULL_TRADE_INTERVAL",),
        )

    entries = tuple(
        entry
        for entry in ledger.entries
        if entry.symbol == account_pnl.symbol
        and record.created_time_ms <= entry.transaction_time_ms <= record.updated_time_ms
    )
    unique = _dedupe(entries)
    funding_net = sum((entry.amount_usdt for entry in unique), start=_ZERO)
    all_in = account_pnl.account_closed_pnl_usdt + funding_net
    return BybitDemoAllInPnlReconciliation(
        status=BybitDemoFundingStatus.FUNDING_RECONCILED,
        symbol=account_pnl.symbol,
        account_closed_pnl_usdt=account_pnl.account_closed_pnl_usdt,
        funding_net_usdt=funding_net,
        all_in_net_pnl_usdt=all_in,
        funding_entry_count=len(unique),
        reasons=(),
        account_closed_pnl_reconciled=True,
        funding_reconciled=True,
        fully_reconciled_net_pnl=True,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )


def apply_funding_to_account_view(
    account_pnl: BybitDemoAccountPnlReconciliation,
    funding: BybitDemoAllInPnlReconciliation,
) -> BybitDemoAccountPnlReconciliation:
    """Return the account reconciliation view used by the lifecycle gate after funding proof."""

    if (
        funding.status is not BybitDemoFundingStatus.FUNDING_RECONCILED
        or not funding.fully_reconciled_net_pnl
    ):
        return account_pnl
    return replace(
        account_pnl,
        funding_reconciled=True,
        fully_reconciled_net_pnl=True,
    )


def _window_covers_trade(
    ledger: BybitDemoFundingLedgerWindow,
    record: BybitDemoClosedPnlRecord,
) -> bool:
    return (
        ledger.coverage_start_ms <= record.created_time_ms
        and ledger.coverage_end_ms >= record.updated_time_ms
    )


def _dedupe(
    entries: Sequence[BybitDemoFundingLedgerEntry],
) -> tuple[BybitDemoFundingLedgerEntry, ...]:
    seen: set[str] = set()
    result: list[BybitDemoFundingLedgerEntry] = []
    for entry in sorted(entries, key=lambda item: (item.transaction_time_ms, item.reference_id)):
        if not entry.reference_id:
            raise ValueError("funding ledger reference_id cannot be empty")
        if entry.reference_id in seen:
            continue
        seen.add(entry.reference_id)
        result.append(entry)
    return tuple(result)


def _result(
    status: BybitDemoFundingStatus,
    account_pnl: BybitDemoAccountPnlReconciliation,
    *,
    funding_net: Decimal | None,
    funding_count: int,
    reasons: tuple[str, ...],
) -> BybitDemoAllInPnlReconciliation:
    return BybitDemoAllInPnlReconciliation(
        status=status,
        symbol=account_pnl.symbol,
        account_closed_pnl_usdt=account_pnl.account_closed_pnl_usdt,
        funding_net_usdt=funding_net,
        all_in_net_pnl_usdt=None,
        funding_entry_count=funding_count,
        reasons=reasons,
        account_closed_pnl_reconciled=account_pnl.account_closed_pnl_reconciled,
        funding_reconciled=False,
        fully_reconciled_net_pnl=False,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )
