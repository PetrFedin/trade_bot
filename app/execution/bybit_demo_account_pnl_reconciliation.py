from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping, Sequence

from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
)

_ZERO = Decimal("0")


class BybitDemoAccountPnlStatus(StrEnum):
    TRADE_NOT_TERMINAL = "TRADE_NOT_TERMINAL"
    CLOSED_PNL_NOT_FOUND = "CLOSED_PNL_NOT_FOUND"
    CLOSED_PNL_AMBIGUOUS = "CLOSED_PNL_AMBIGUOUS"
    CLOSED_PNL_RECONCILED = "CLOSED_PNL_RECONCILED"
    CLOSED_PNL_MISMATCH = "CLOSED_PNL_MISMATCH"


@dataclass(frozen=True)
class BybitDemoClosedPnlRecord:
    symbol: str
    side: str
    quantity: Decimal
    average_entry_price: Decimal
    average_exit_price: Decimal
    closed_pnl_usdt: Decimal
    open_fee_usdt: Decimal | None
    close_fee_usdt: Decimal | None
    created_time_ms: int
    updated_time_ms: int


@dataclass(frozen=True)
class BybitDemoAccountPnlReconciliationPolicy:
    quantity_tolerance: Decimal = Decimal("0.000000000001")
    price_tolerance_fraction: Decimal = Decimal("0.0001")
    pnl_tolerance_usdt: Decimal = Decimal("0.05")

    def validate(self) -> None:
        if self.quantity_tolerance < 0:
            raise ValueError("closed-PnL quantity tolerance cannot be negative")
        if not _ZERO <= self.price_tolerance_fraction < Decimal("1"):
            raise ValueError("closed-PnL price tolerance fraction must be within [0, 1)")
        if self.pnl_tolerance_usdt < 0:
            raise ValueError("closed-PnL PnL tolerance cannot be negative")


@dataclass(frozen=True)
class BybitDemoAccountPnlReconciliation:
    status: BybitDemoAccountPnlStatus
    symbol: str
    matched_record: BybitDemoClosedPnlRecord | None
    fill_net_after_execution_fees_usdt: Decimal | None
    account_closed_pnl_usdt: Decimal | None
    account_minus_fill_net_usdt: Decimal | None
    execution_fee_difference_usdt: Decimal | None
    reasons: tuple[str, ...]
    account_closed_pnl_reconciled: bool
    funding_reconciled: bool
    fully_reconciled_net_pnl: bool
    next_entry_allowed: bool
    strategy_promotion_allowed: bool = False
    demo_only: bool = True
    live_mainnet_order_routing_allowed: bool = False


def parse_bybit_demo_closed_pnl_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[BybitDemoClosedPnlRecord, ...]:
    """Normalize closed-PnL rows without claiming funding-inclusive economics."""

    records = tuple(_parse_row(row) for row in rows)
    return tuple(
        sorted(
            records,
            key=lambda item: (item.updated_time_ms, item.created_time_ms),
        )
    )


def reconcile_bybit_demo_account_pnl(
    trade: BybitDemoTradeMonitorResult,
    closed_pnl_rows: Sequence[Mapping[str, Any]],
    *,
    policy: BybitDemoAccountPnlReconciliationPolicy | None = None,
) -> BybitDemoAccountPnlReconciliation:
    """Cross-check a terminal fill-level trade against one account closed-PnL record.

    A successful match reconciles the exchange closed-PnL view but deliberately leaves funding
    unresolved. ``fully_reconciled_net_pnl`` therefore remains false until a separate funding
    ledger is incorporated. This prevents a fill/closed-PnL agreement from being mislabeled as
    all-in profitability.
    """

    active = (
        BybitDemoAccountPnlReconciliationPolicy() if policy is None else policy
    )
    active.validate()
    if (
        trade.status is not BybitDemoTradeMonitorStatus.CLOSED_RECONCILED
        or not trade.terminal
        or not trade.next_entry_allowed
    ):
        return _result(
            BybitDemoAccountPnlStatus.TRADE_NOT_TERMINAL,
            trade,
            reasons=("FILL_LEVEL_TRADE_NOT_TERMINAL",),
        )
    if (
        trade.average_entry_price is None
        or trade.average_exit_price is None
        or trade.realized_net_pnl_after_execution_fees_usdt is None
    ):
        return _result(
            BybitDemoAccountPnlStatus.TRADE_NOT_TERMINAL,
            trade,
            reasons=("TERMINAL_FILL_ECONOMICS_INCOMPLETE",),
        )

    records = parse_bybit_demo_closed_pnl_rows(closed_pnl_rows)
    matches = tuple(
        record
        for record in records
        if _matches_trade(record, trade, policy=active)
    )
    if not matches:
        return _result(
            BybitDemoAccountPnlStatus.CLOSED_PNL_NOT_FOUND,
            trade,
            reasons=("MATCHING_ACCOUNT_CLOSED_PNL_NOT_FOUND",),
        )
    if len(matches) != 1:
        return _result(
            BybitDemoAccountPnlStatus.CLOSED_PNL_AMBIGUOUS,
            trade,
            reasons=("MULTIPLE_ACCOUNT_CLOSED_PNL_MATCHES",),
        )

    record = matches[0]
    fill_net = trade.realized_net_pnl_after_execution_fees_usdt
    delta = record.closed_pnl_usdt - fill_net
    exchange_fee_total = None
    fee_delta = None
    if record.open_fee_usdt is not None and record.close_fee_usdt is not None:
        exchange_fee_total = record.open_fee_usdt + record.close_fee_usdt
        fee_delta = exchange_fee_total - trade.execution_fees_usdt
    reasons: list[str] = []
    if abs(delta) > active.pnl_tolerance_usdt:
        reasons.append("ACCOUNT_CLOSED_PNL_DIFFERS_FROM_FILL_NET")
    if fee_delta is not None and abs(fee_delta) > active.pnl_tolerance_usdt:
        reasons.append("ACCOUNT_EXECUTION_FEES_DIFFER_FROM_FILL_FEES")

    reconciled = not reasons
    status = (
        BybitDemoAccountPnlStatus.CLOSED_PNL_RECONCILED
        if reconciled
        else BybitDemoAccountPnlStatus.CLOSED_PNL_MISMATCH
    )
    return BybitDemoAccountPnlReconciliation(
        status=status,
        symbol=trade.symbol,
        matched_record=record,
        fill_net_after_execution_fees_usdt=fill_net,
        account_closed_pnl_usdt=record.closed_pnl_usdt,
        account_minus_fill_net_usdt=delta,
        execution_fee_difference_usdt=fee_delta,
        reasons=tuple(reasons),
        account_closed_pnl_reconciled=reconciled,
        funding_reconciled=False,
        fully_reconciled_net_pnl=False,
        next_entry_allowed=trade.next_entry_allowed,
        strategy_promotion_allowed=False,
        demo_only=True,
        live_mainnet_order_routing_allowed=False,
    )


def _matches_trade(
    record: BybitDemoClosedPnlRecord,
    trade: BybitDemoTradeMonitorResult,
    *,
    policy: BybitDemoAccountPnlReconciliationPolicy,
) -> bool:
    if record.symbol != trade.symbol or record.side != trade.entry_side:
        return False
    if abs(record.quantity - trade.entry_quantity) > policy.quantity_tolerance:
        return False
    if trade.average_entry_price is None or trade.average_exit_price is None:
        return False
    if not _price_close(
        record.average_entry_price,
        trade.average_entry_price,
        tolerance_fraction=policy.price_tolerance_fraction,
    ):
        return False
    return _price_close(
        record.average_exit_price,
        trade.average_exit_price,
        tolerance_fraction=policy.price_tolerance_fraction,
    )


def _price_close(
    left: Decimal,
    right: Decimal,
    *,
    tolerance_fraction: Decimal,
) -> bool:
    scale = max(abs(left), abs(right), Decimal("1"))
    return abs(left - right) <= scale * tolerance_fraction


def _parse_row(row: Mapping[str, Any]) -> BybitDemoClosedPnlRecord:
    symbol = _text(row, "symbol")
    side = _text(row, "side")
    if side not in {"Buy", "Sell"}:
        raise ValueError("Bybit closed-PnL side must be Buy or Sell")
    return BybitDemoClosedPnlRecord(
        symbol=symbol,
        side=side,
        quantity=_decimal(row, "qty"),
        average_entry_price=_decimal(row, "avgEntryPrice"),
        average_exit_price=_decimal(row, "avgExitPrice"),
        closed_pnl_usdt=_decimal(row, "closedPnl", allow_negative=True),
        open_fee_usdt=_optional_decimal(row, "openFee", allow_negative=True),
        close_fee_usdt=_optional_decimal(row, "closeFee", allow_negative=True),
        created_time_ms=_integer(row, "createdTime"),
        updated_time_ms=_integer(row, "updatedTime"),
    )


def _result(
    status: BybitDemoAccountPnlStatus,
    trade: BybitDemoTradeMonitorResult,
    *,
    reasons: tuple[str, ...],
) -> BybitDemoAccountPnlReconciliation:
    return BybitDemoAccountPnlReconciliation(
        status=status,
        symbol=trade.symbol,
        matched_record=None,
        fill_net_after_execution_fees_usdt=(
            trade.realized_net_pnl_after_execution_fees_usdt
        ),
        account_closed_pnl_usdt=None,
        account_minus_fill_net_usdt=None,
        execution_fee_difference_usdt=None,
        reasons=reasons,
        account_closed_pnl_reconciled=False,
        funding_reconciled=False,
        fully_reconciled_net_pnl=False,
        next_entry_allowed=trade.next_entry_allowed,
        strategy_promotion_allowed=False,
        demo_only=True,
        live_mainnet_order_routing_allowed=False,
    )


def _text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Bybit closed-PnL missing {field}")
    return value


def _decimal(
    row: Mapping[str, Any],
    field: str,
    *,
    allow_negative: bool = False,
) -> Decimal:
    value = row.get(field)
    if value is None or value == "":
        raise ValueError(f"Bybit closed-PnL missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Bybit closed-PnL invalid {field}") from exc
    if not parsed.is_finite() or (not allow_negative and parsed < 0):
        raise ValueError(f"Bybit closed-PnL invalid {field}")
    return parsed


def _optional_decimal(
    row: Mapping[str, Any],
    field: str,
    *,
    allow_negative: bool = False,
) -> Decimal | None:
    value = row.get(field)
    if value is None or value == "":
        return None
    return _decimal(row, field, allow_negative=allow_negative)


def _integer(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit closed-PnL invalid {field}") from exc
    if parsed < 0:
        raise ValueError(f"Bybit closed-PnL invalid {field}")
    return parsed
