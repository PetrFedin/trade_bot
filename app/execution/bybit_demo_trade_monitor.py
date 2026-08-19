from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from app.execution.bybit_demo import BybitDemoPosition

_ZERO = Decimal("0")
_QUANTITY_EPSILON = Decimal("0.000000000001")


class BybitDemoTradeMonitorStatus(StrEnum):
    ENTRY_FILL_UNRESOLVED = "ENTRY_FILL_UNRESOLVED"
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED_RECONCILED = "CLOSED_RECONCILED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class BybitDemoExecutionFill:
    exec_id: str
    order_link_id: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    exec_time_ms: int


@dataclass(frozen=True)
class BybitDemoTradeMonitorResult:
    status: BybitDemoTradeMonitorStatus
    symbol: str
    entry_order_link_id: str
    entry_side: str
    entry_quantity: Decimal
    exit_quantity: Decimal
    remaining_quantity: Decimal
    average_entry_price: Decimal | None
    average_exit_price: Decimal | None
    entry_fees_usdt: Decimal
    exit_fees_usdt: Decimal
    execution_fees_usdt: Decimal
    realized_gross_pnl_usdt: Decimal | None
    realized_net_pnl_after_execution_fees_usdt: Decimal | None
    reasons: tuple[str, ...]
    terminal: bool
    next_entry_allowed: bool
    funding_reconciled: bool = False
    account_closed_pnl_reconciled: bool = False
    demo_only: bool = True
    live_mainnet_order_routing_allowed: bool = False


class _DemoReadClient(Protocol):
    @property
    def live_mainnet_order_routing_allowed(self) -> bool: ...

    def get_positions(self, *, settle_coin: str = "USDT") -> tuple[BybitDemoPosition, ...]: ...

    def get_executions(
        self,
        *,
        symbol: str,
        order_link_id: str | None = None,
        limit: int = 50,
    ) -> tuple[Mapping[str, Any], ...]: ...


def reconcile_bybit_demo_trade(
    *,
    client: _DemoReadClient,
    symbol: str,
    entry_side: str,
    entry_order_link_id: str,
    execution_limit: int = 100,
) -> BybitDemoTradeMonitorResult:
    """Reconcile one demo trade from fills and current position state.

    The monitor deliberately does not claim funding-complete account PnL. It proves only the
    fill-level gross PnL and execution fees available from the execution stream. A symbol is
    eligible for another entry only when the current position is absent and opposite-side exit
    fills exactly cover the reconciled entry quantity. Missing/overlapping evidence fails closed.
    """

    if client.live_mainnet_order_routing_allowed:
        raise ValueError("demo trade monitor rejected a client that permits mainnet routing")
    _validate_symbol(symbol)
    if entry_side not in {"Buy", "Sell"}:
        raise ValueError("entry side must be Buy or Sell")
    if not entry_order_link_id.startswith("ASTRA-DEMO-"):
        raise ValueError("entry orderLinkId must use ASTRA-DEMO- namespace")
    if not 1 <= execution_limit <= 100:
        raise ValueError("execution_limit must be within [1, 100]")

    entry_rows = client.get_executions(
        symbol=symbol,
        order_link_id=entry_order_link_id,
        limit=execution_limit,
    )
    entry_fills = _normalize_fills(entry_rows, symbol=symbol)
    entry_fills = tuple(
        fill
        for fill in entry_fills
        if fill.order_link_id == entry_order_link_id and fill.side == entry_side
    )
    if not entry_fills:
        return _empty_result(
            status=BybitDemoTradeMonitorStatus.ENTRY_FILL_UNRESOLVED,
            symbol=symbol,
            entry_side=entry_side,
            entry_order_link_id=entry_order_link_id,
            reasons=("ENTRY_EXECUTION_FILL_NOT_FOUND",),
        )

    entry_quantity = sum((fill.quantity for fill in entry_fills), start=_ZERO)
    entry_fees = sum((fill.fee for fill in entry_fills), start=_ZERO)
    average_entry = _weighted_average(entry_fills)
    first_entry_time = min(fill.exec_time_ms for fill in entry_fills)

    all_rows = client.get_executions(symbol=symbol, limit=execution_limit)
    all_fills = _normalize_fills(all_rows, symbol=symbol)
    exit_side = "Sell" if entry_side == "Buy" else "Buy"
    exit_fills = tuple(
        fill
        for fill in all_fills
        if fill.side == exit_side and fill.exec_time_ms >= first_entry_time
    )
    exit_fills = _fills_until_quantity(exit_fills, maximum_quantity=entry_quantity)
    exit_quantity = sum((fill.quantity for fill in exit_fills), start=_ZERO)
    exit_fees = sum((fill.fee for fill in exit_fills), start=_ZERO)
    remaining = max(_ZERO, entry_quantity - exit_quantity)
    average_exit = _weighted_average(exit_fills) if exit_fills else None

    positions = client.get_positions(settle_coin="USDT")
    open_positions = tuple(
        position for position in positions if position.symbol == symbol and position.size > 0
    )
    reasons: list[str] = []
    if len(open_positions) > 1:
        reasons.append("MULTIPLE_OPEN_SYMBOL_POSITIONS")
    current_position = open_positions[0] if len(open_positions) == 1 else None
    if current_position is not None:
        expected_remaining = current_position.size
        if abs(expected_remaining - remaining) > _QUANTITY_EPSILON:
            reasons.append("POSITION_AND_EXECUTION_QUANTITY_MISMATCH")

    if exit_quantity - entry_quantity > _QUANTITY_EPSILON:
        reasons.append("EXIT_QUANTITY_EXCEEDS_ENTRY")

    execution_fees = entry_fees + exit_fees
    gross_pnl = None
    net_pnl = None
    if exit_quantity > 0 and average_exit is not None:
        matched_quantity = min(entry_quantity, exit_quantity)
        if entry_side == "Buy":
            gross_pnl = (average_exit - average_entry) * matched_quantity
        else:
            gross_pnl = (average_entry - average_exit) * matched_quantity
        realized_entry_fees = entry_fees * matched_quantity / entry_quantity
        realized_execution_fees = realized_entry_fees + exit_fees
        net_pnl = gross_pnl - realized_execution_fees

    if reasons:
        status = BybitDemoTradeMonitorStatus.AMBIGUOUS
        terminal = False
    elif current_position is not None:
        status = (
            BybitDemoTradeMonitorStatus.PARTIALLY_CLOSED
            if exit_quantity > 0
            else BybitDemoTradeMonitorStatus.OPEN
        )
        terminal = False
    elif abs(exit_quantity - entry_quantity) <= _QUANTITY_EPSILON:
        status = BybitDemoTradeMonitorStatus.CLOSED_RECONCILED
        terminal = True
    else:
        status = BybitDemoTradeMonitorStatus.AMBIGUOUS
        terminal = False
        reasons.append("EXECUTION_WINDOW_NOT_PROVEN_COMPLETE")

    return BybitDemoTradeMonitorResult(
        status=status,
        symbol=symbol,
        entry_order_link_id=entry_order_link_id,
        entry_side=entry_side,
        entry_quantity=entry_quantity,
        exit_quantity=exit_quantity,
        remaining_quantity=remaining,
        average_entry_price=average_entry,
        average_exit_price=average_exit,
        entry_fees_usdt=entry_fees,
        exit_fees_usdt=exit_fees,
        execution_fees_usdt=execution_fees,
        realized_gross_pnl_usdt=gross_pnl,
        realized_net_pnl_after_execution_fees_usdt=net_pnl,
        reasons=tuple(reasons),
        terminal=terminal,
        next_entry_allowed=terminal,
    )


def _normalize_fills(
    rows: tuple[Mapping[str, Any], ...],
    *,
    symbol: str,
) -> tuple[BybitDemoExecutionFill, ...]:
    fills: list[BybitDemoExecutionFill] = []
    seen_exec_ids: set[str] = set()
    for row in rows:
        row_symbol = row.get("symbol")
        if row_symbol not in (None, symbol):
            continue
        exec_id = _required_text(row, "execId")
        if exec_id in seen_exec_ids:
            continue
        side = _required_text(row, "side")
        if side not in {"Buy", "Sell"}:
            raise ValueError("Bybit demo execution side must be Buy or Sell")
        quantity = _required_decimal(row, "execQty")
        price = _required_decimal(row, "execPrice")
        fee = _required_decimal(row, "execFee", allow_negative=True)
        exec_time = _required_int(row, "execTime")
        if quantity <= 0 or price <= 0:
            raise ValueError("Bybit demo execution quantity and price must be positive")
        fills.append(
            BybitDemoExecutionFill(
                exec_id=exec_id,
                order_link_id=str(row.get("orderLinkId") or ""),
                side=side,
                quantity=quantity,
                price=price,
                fee=fee,
                exec_time_ms=exec_time,
            )
        )
        seen_exec_ids.add(exec_id)
    return tuple(sorted(fills, key=lambda item: (item.exec_time_ms, item.exec_id)))


def _fills_until_quantity(
    fills: tuple[BybitDemoExecutionFill, ...],
    *,
    maximum_quantity: Decimal,
) -> tuple[BybitDemoExecutionFill, ...]:
    selected: list[BybitDemoExecutionFill] = []
    cumulative = _ZERO
    for fill in fills:
        selected.append(fill)
        cumulative += fill.quantity
        if cumulative + _QUANTITY_EPSILON >= maximum_quantity:
            break
    return tuple(selected)


def _weighted_average(fills: tuple[BybitDemoExecutionFill, ...]) -> Decimal:
    quantity = sum((fill.quantity for fill in fills), start=_ZERO)
    if quantity <= 0:
        raise ValueError("weighted average requires positive execution quantity")
    value = sum((fill.price * fill.quantity for fill in fills), start=_ZERO)
    return value / quantity


def _empty_result(
    *,
    status: BybitDemoTradeMonitorStatus,
    symbol: str,
    entry_side: str,
    entry_order_link_id: str,
    reasons: tuple[str, ...],
) -> BybitDemoTradeMonitorResult:
    return BybitDemoTradeMonitorResult(
        status=status,
        symbol=symbol,
        entry_order_link_id=entry_order_link_id,
        entry_side=entry_side,
        entry_quantity=_ZERO,
        exit_quantity=_ZERO,
        remaining_quantity=_ZERO,
        average_entry_price=None,
        average_exit_price=None,
        entry_fees_usdt=_ZERO,
        exit_fees_usdt=_ZERO,
        execution_fees_usdt=_ZERO,
        realized_gross_pnl_usdt=None,
        realized_net_pnl_after_execution_fees_usdt=None,
        reasons=reasons,
        terminal=False,
        next_entry_allowed=False,
    )


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Bybit demo execution missing {field}")
    return value


def _required_decimal(
    row: Mapping[str, Any],
    field: str,
    *,
    allow_negative: bool = False,
) -> Decimal:
    value = row.get(field)
    if value is None or value == "":
        raise ValueError(f"Bybit demo execution missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Bybit demo execution invalid {field}") from exc
    if not parsed.is_finite() or (not allow_negative and parsed < 0):
        raise ValueError(f"Bybit demo execution invalid {field}")
    return parsed


def _required_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool):
        raise ValueError(f"Bybit demo execution invalid {field}")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit demo execution invalid {field}") from exc
    if parsed < 0:
        raise ValueError(f"Bybit demo execution invalid {field}")
    return parsed


def _validate_symbol(symbol: str) -> None:
    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise ValueError("Bybit symbol must be normalized USDT linear symbol")
