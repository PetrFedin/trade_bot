from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.execution.bybit_mainnet_readonly import (
    BybitMainnetReadOnlyClient,
    BybitMainnetReadOnlyError,
    validate_bybit_mainnet_readonly_host,
)
from app.execution.bybit_rest_policy import BybitRestProtocolError

_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
_DEFAULT_WINDOW_MS = 24 * 60 * 60 * 1000
_ZERO = Decimal("0")


class BybitMainnetActivityError(RuntimeError):
    """Raised when real-account broker activity is internally inconsistent."""


@dataclass(frozen=True)
class BybitMainnetActivityWindow:
    start_time_ms: int
    end_time_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_time_ms - self.start_time_ms

    def validate(self) -> None:
        for name, value in (
            ("start_time_ms", self.start_time_ms),
            ("end_time_ms", self.end_time_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Bybit activity {name} must be non-negative integer ms")
        if self.end_time_ms <= self.start_time_ms:
            raise ValueError("Bybit activity window must have positive duration")
        if self.duration_ms > _MAX_WINDOW_MS:
            raise ValueError("Bybit activity window cannot exceed 7 days")

    @classmethod
    def last_24_hours_ending_at(cls, end_time_ms: int) -> BybitMainnetActivityWindow:
        if (
            isinstance(end_time_ms, bool)
            or not isinstance(end_time_ms, int)
            or end_time_ms < _DEFAULT_WINDOW_MS
        ):
            raise ValueError("Bybit activity end time cannot form a 24-hour window")
        value = cls(end_time_ms - _DEFAULT_WINDOW_MS, end_time_ms)
        value.validate()
        return value


@dataclass(frozen=True)
class BybitMainnetExecutionRecord:
    symbol: str
    exec_id: str
    order_id: str
    order_link_id: str
    side: str
    order_type: str
    exec_type: str
    exec_time_ms: int
    exec_price: Decimal
    exec_qty: Decimal
    exec_value: Decimal
    exec_fee: Decimal
    fee_currency: str | None
    fee_rate: Decimal
    is_maker: bool
    leaves_qty: Decimal
    closed_size: Decimal | None
    seq: int

    def validate(self, *, window: BybitMainnetActivityWindow) -> None:
        window.validate()
        _validate_usdt_symbol(self.symbol)
        for name, value in (
            ("exec_id", self.exec_id),
            ("order_id", self.order_id),
            ("order_type", self.order_type),
        ):
            _validate_required_text(value, name=name)
        if self.order_link_id != self.order_link_id.strip():
            raise ValueError("Bybit execution order_link_id must be normalized")
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("Bybit execution side must be Buy or Sell")
        if self.exec_type != "Trade":
            raise ValueError("Bybit typed execution record must be a Trade execution")
        if (
            isinstance(self.exec_time_ms, bool)
            or not isinstance(self.exec_time_ms, int)
            or not window.start_time_ms <= self.exec_time_ms <= window.end_time_ms
        ):
            raise ValueError("Bybit execution time is outside the requested window")
        for name, value in (
            ("exec_price", self.exec_price),
            ("exec_qty", self.exec_qty),
            ("exec_value", self.exec_value),
            ("exec_fee", self.exec_fee),
            ("fee_rate", self.fee_rate),
            ("leaves_qty", self.leaves_qty),
        ):
            if not value.is_finite():
                raise ValueError(f"Bybit execution {name} must be finite")
        if self.exec_price <= _ZERO or self.exec_qty <= _ZERO or self.exec_value <= _ZERO:
            raise ValueError("Bybit execution price, quantity and value must be positive")
        if self.leaves_qty < _ZERO:
            raise ValueError("Bybit execution leaves quantity cannot be negative")
        if self.closed_size is not None and (
            not self.closed_size.is_finite() or self.closed_size < _ZERO
        ):
            raise ValueError("Bybit execution closed size must be non-negative and finite")
        if self.fee_currency is not None:
            if (
                not self.fee_currency
                or self.fee_currency != self.fee_currency.strip().upper()
                or not self.fee_currency.isalnum()
            ):
                raise ValueError("Bybit execution fee currency must be normalized uppercase text")
        if not isinstance(self.is_maker, bool):
            raise ValueError("Bybit execution maker flag must be boolean")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise ValueError("Bybit execution seq must be non-negative integer")


@dataclass(frozen=True)
class BybitMainnetClosedPnlRecord:
    symbol: str
    order_id: str
    side: str
    order_type: str
    exec_type: str
    qty: Decimal
    closed_size: Decimal
    cumulative_entry_value: Decimal
    average_entry_price: Decimal
    cumulative_exit_value: Decimal
    average_exit_price: Decimal
    closed_pnl: Decimal
    fill_count: int
    leverage: Decimal
    open_fee: Decimal | None
    close_fee: Decimal | None
    created_time_ms: int
    updated_time_ms: int

    def validate(self, *, window: BybitMainnetActivityWindow) -> None:
        window.validate()
        _validate_usdt_symbol(self.symbol)
        _validate_required_text(self.order_id, name="order_id")
        _validate_required_text(self.order_type, name="order_type")
        _validate_required_text(self.exec_type, name="exec_type")
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("Bybit closed PnL side must be Buy or Sell")
        for name, value in (
            ("qty", self.qty),
            ("closed_size", self.closed_size),
            ("cumulative_entry_value", self.cumulative_entry_value),
            ("average_entry_price", self.average_entry_price),
            ("cumulative_exit_value", self.cumulative_exit_value),
            ("average_exit_price", self.average_exit_price),
            ("closed_pnl", self.closed_pnl),
            ("leverage", self.leverage),
        ):
            if not value.is_finite():
                raise ValueError(f"Bybit closed PnL {name} must be finite")
        if self.qty <= _ZERO or self.closed_size <= _ZERO:
            raise ValueError("Bybit closed PnL quantity and closed size must be positive")
        if self.average_entry_price <= _ZERO or self.average_exit_price <= _ZERO:
            raise ValueError("Bybit closed PnL average prices must be positive")
        if self.cumulative_entry_value < _ZERO or self.cumulative_exit_value < _ZERO:
            raise ValueError("Bybit closed PnL cumulative values cannot be negative")
        if self.leverage <= _ZERO:
            raise ValueError("Bybit closed PnL leverage must be positive")
        for name, value in (("open_fee", self.open_fee), ("close_fee", self.close_fee)):
            if value is not None and not value.is_finite():
                raise ValueError(f"Bybit closed PnL {name} must be finite when present")
        if isinstance(self.fill_count, bool) or not isinstance(self.fill_count, int):
            raise ValueError("Bybit closed PnL fill count must be integer")
        if self.fill_count < 0:
            raise ValueError("Bybit closed PnL fill count cannot be negative")
        for name, value in (
            ("created_time_ms", self.created_time_ms),
            ("updated_time_ms", self.updated_time_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Bybit closed PnL {name} must be non-negative integer ms")
        if self.updated_time_ms < self.created_time_ms:
            raise ValueError("Bybit closed PnL updated time cannot precede created time")
        if not window.start_time_ms <= self.updated_time_ms <= window.end_time_ms:
            raise ValueError("Bybit closed PnL updated time is outside the requested window")


@dataclass(frozen=True)
class BybitMainnetTransactionRecord:
    transaction_id: str
    transaction_time_ms: int
    transaction_type: str
    transaction_sub_type: str
    category: str
    currency: str
    symbol: str | None
    side: str | None
    trade_id: str | None
    order_id: str | None
    order_link_id: str | None
    qty: Decimal | None
    size: Decimal | None
    trade_price: Decimal | None
    funding: Decimal
    fee: Decimal
    cash_flow: Decimal
    change: Decimal
    cash_balance: Decimal
    fee_rate: Decimal | None

    @property
    def broker_identity(self) -> tuple[str, int, str | None, str | None, str]:
        return (
            self.transaction_id,
            self.transaction_time_ms,
            self.trade_id,
            self.order_id,
            self.transaction_type,
        )

    def validate(self, *, window: BybitMainnetActivityWindow) -> None:
        window.validate()
        _validate_required_text(self.transaction_id, name="transaction_id")
        _validate_required_text(self.transaction_type, name="transaction_type")
        if self.transaction_sub_type != self.transaction_sub_type.strip():
            raise ValueError("Bybit transaction sub-type must be normalized")
        if self.category != "linear":
            raise ValueError("Bybit typed transaction category must be linear")
        if self.currency != "USDT":
            raise ValueError("Bybit typed transaction currency must be USDT")
        if self.symbol is not None:
            _validate_usdt_symbol(self.symbol)
        if self.side not in {None, "Buy", "Sell"}:
            raise ValueError("Bybit transaction side must be Buy, Sell or None")
        if (
            isinstance(self.transaction_time_ms, bool)
            or not isinstance(self.transaction_time_ms, int)
            or not window.start_time_ms <= self.transaction_time_ms <= window.end_time_ms
        ):
            raise ValueError("Bybit transaction time is outside the requested window")
        for name, value in (
            ("funding", self.funding),
            ("fee", self.fee),
            ("cash_flow", self.cash_flow),
            ("change", self.change),
            ("cash_balance", self.cash_balance),
        ):
            if not value.is_finite():
                raise ValueError(f"Bybit transaction {name} must be finite")
        for name, value in (
            ("qty", self.qty),
            ("size", self.size),
            ("trade_price", self.trade_price),
            ("fee_rate", self.fee_rate),
        ):
            if value is not None and not value.is_finite():
                raise ValueError(f"Bybit transaction {name} must be finite when present")
        for name, value in (
            ("trade_id", self.trade_id),
            ("order_id", self.order_id),
            ("order_link_id", self.order_link_id),
        ):
            if value is not None and (not value or value != value.strip()):
                raise ValueError(f"Bybit transaction {name} must be normalized when present")
        if self.change != self.cash_flow + self.funding - self.fee:
            raise BybitMainnetActivityError(
                "Bybit transaction violates change = cashFlow + funding - fee"
            )


@dataclass(frozen=True)
class BybitMainnetActivitySnapshot:
    window: BybitMainnetActivityWindow
    api_host: str
    api_key_fingerprint_sha256: str
    executions: tuple[BybitMainnetExecutionRecord, ...]
    closed_pnl: tuple[BybitMainnetClosedPnlRecord, ...]
    transactions: tuple[BybitMainnetTransactionRecord, ...]
    excluded_non_trade_execution_count: int
    excluded_non_usdt_closed_pnl_count: int
    transaction_cash_flow_usdt: Decimal
    transaction_funding_usdt: Decimal
    transaction_fee_usdt: Decimal
    transaction_change_usdt: Decimal
    environment: str = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed: bool = False
    order_writes_supported: bool = False

    def validate(self) -> None:
        self.window.validate()
        validate_bybit_mainnet_readonly_host(self.api_host)
        if len(self.api_key_fingerprint_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.api_key_fingerprint_sha256
        ):
            raise ValueError("Bybit activity API-key fingerprint must be sha256 hex")
        for record in self.executions:
            record.validate(window=self.window)
        for record in self.closed_pnl:
            record.validate(window=self.window)
        for record in self.transactions:
            record.validate(window=self.window)
        for name, value in (
            ("excluded_non_trade_execution_count", self.excluded_non_trade_execution_count),
            ("excluded_non_usdt_closed_pnl_count", self.excluded_non_usdt_closed_pnl_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Bybit activity {name} must be non-negative integer")
        totals = (
            self.transaction_cash_flow_usdt,
            self.transaction_funding_usdt,
            self.transaction_fee_usdt,
            self.transaction_change_usdt,
        )
        if any(not value.is_finite() for value in totals):
            raise ValueError("Bybit activity transaction totals must be finite")
        if self.transaction_cash_flow_usdt != sum(
            (item.cash_flow for item in self.transactions), start=_ZERO
        ):
            raise ValueError("Bybit activity cash-flow total does not match transaction rows")
        if self.transaction_funding_usdt != sum(
            (item.funding for item in self.transactions), start=_ZERO
        ):
            raise ValueError("Bybit activity funding total does not match transaction rows")
        if self.transaction_fee_usdt != sum(
            (item.fee for item in self.transactions), start=_ZERO
        ):
            raise ValueError("Bybit activity fee total does not match transaction rows")
        if self.transaction_change_usdt != sum(
            (item.change for item in self.transactions), start=_ZERO
        ):
            raise ValueError("Bybit activity change total does not match transaction rows")
        if self.transaction_change_usdt != (
            self.transaction_cash_flow_usdt
            + self.transaction_funding_usdt
            - self.transaction_fee_usdt
        ):
            raise ValueError("Bybit activity aggregate transaction accounting is inconsistent")
        if self.environment != "BYBIT_MAINNET_READONLY":
            raise ValueError("Bybit activity environment is invalid")
        if self.live_mainnet_order_routing_allowed or self.order_writes_supported:
            raise ValueError("Bybit activity snapshot cannot grant order writes")

    def to_safe_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "environment": self.environment,
            "api_host": self.api_host,
            "api_key_fingerprint_sha256": self.api_key_fingerprint_sha256,
            "live_mainnet_order_routing_allowed": False,
            "order_writes_supported": False,
            "window": {
                "start_time_ms": self.window.start_time_ms,
                "end_time_ms": self.window.end_time_ms,
                "duration_ms": self.window.duration_ms,
            },
            "summary": {
                "execution_count": len(self.executions),
                "closed_pnl_count": len(self.closed_pnl),
                "transaction_count": len(self.transactions),
                "excluded_non_trade_execution_count": self.excluded_non_trade_execution_count,
                "excluded_non_usdt_closed_pnl_count": self.excluded_non_usdt_closed_pnl_count,
                "transaction_cash_flow_usdt": _decimal_text(
                    self.transaction_cash_flow_usdt
                ),
                "transaction_funding_usdt": _decimal_text(self.transaction_funding_usdt),
                "transaction_fee_usdt": _decimal_text(self.transaction_fee_usdt),
                "transaction_change_usdt": _decimal_text(self.transaction_change_usdt),
            },
            "executions": [_execution_safe_dict(item) for item in self.executions],
            "closed_pnl": [_closed_pnl_safe_dict(item) for item in self.closed_pnl],
            "transactions": [_transaction_safe_dict(item) for item in self.transactions],
        }


class BybitMainnetReadOnlyActivityClient(BybitMainnetReadOnlyClient):
    """Typed activity reads layered on the qualified zero-mutation mainnet client."""

    def read_execution_rows(
        self,
        window: BybitMainnetActivityWindow,
        *,
        max_pages: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        window.validate()
        return self._paginate(
            path="/v5/execution/list",
            base_query={
                "category": "linear",
                "settleCoin": "USDT",
                "startTime": str(window.start_time_ms),
                "endTime": str(window.end_time_ms),
                "limit": "100",
            },
            max_pages=max_pages,
        )

    def read_closed_pnl_rows(
        self,
        window: BybitMainnetActivityWindow,
        *,
        max_pages: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        window.validate()
        return self._paginate(
            path="/v5/position/closed-pnl",
            base_query={
                "category": "linear",
                "startTime": str(window.start_time_ms),
                "endTime": str(window.end_time_ms),
                "limit": "100",
            },
            max_pages=max_pages,
        )

    def read_transaction_rows(
        self,
        window: BybitMainnetActivityWindow,
        *,
        max_pages: int = 100,
    ) -> tuple[Mapping[str, Any], ...]:
        window.validate()
        return self._paginate(
            path="/v5/account/transaction-log",
            base_query={
                "accountType": "UNIFIED",
                "category": "linear",
                "currency": "USDT",
                "startTime": str(window.start_time_ms),
                "endTime": str(window.end_time_ms),
                "limit": "50",
            },
            max_pages=max_pages,
        )


def read_bybit_mainnet_activity(
    client: BybitMainnetReadOnlyActivityClient,
    *,
    window: BybitMainnetActivityWindow,
) -> BybitMainnetActivitySnapshot:
    if client.live_mainnet_order_routing_allowed or client.order_writes_supported:
        raise BybitMainnetReadOnlyError("Bybit activity reader rejected mutation-capable client")
    window.validate()
    key_info = client.verify_read_only_api_key(require_ip_binding=True)
    execution_rows = client.read_execution_rows(window)
    closed_rows = client.read_closed_pnl_rows(window)
    transaction_rows = client.read_transaction_rows(window)

    executions, excluded_non_trade = _parse_executions(execution_rows, window=window)
    closed_pnl, excluded_non_usdt = _parse_closed_pnl(closed_rows, window=window)
    transactions = _parse_transactions(transaction_rows, window=window)

    snapshot = BybitMainnetActivitySnapshot(
        window=window,
        api_host=client.host,
        api_key_fingerprint_sha256=key_info.key_fingerprint_sha256,
        executions=executions,
        closed_pnl=closed_pnl,
        transactions=transactions,
        excluded_non_trade_execution_count=excluded_non_trade,
        excluded_non_usdt_closed_pnl_count=excluded_non_usdt,
        transaction_cash_flow_usdt=sum(
            (item.cash_flow for item in transactions), start=_ZERO
        ),
        transaction_funding_usdt=sum((item.funding for item in transactions), start=_ZERO),
        transaction_fee_usdt=sum((item.fee for item in transactions), start=_ZERO),
        transaction_change_usdt=sum((item.change for item in transactions), start=_ZERO),
    )
    snapshot.validate()
    return snapshot


def _parse_executions(
    rows: tuple[Mapping[str, Any], ...],
    *,
    window: BybitMainnetActivityWindow,
) -> tuple[tuple[BybitMainnetExecutionRecord, ...], int]:
    records: dict[str, BybitMainnetExecutionRecord] = {}
    excluded_non_trade = 0
    for row in rows:
        exec_type = _required_text(row, "execType")
        if exec_type != "Trade":
            excluded_non_trade += 1
            continue
        item = BybitMainnetExecutionRecord(
            symbol=_required_text(row, "symbol"),
            exec_id=_required_text(row, "execId"),
            order_id=_required_text(row, "orderId"),
            order_link_id=_optional_text(row, "orderLinkId") or "",
            side=_required_text(row, "side"),
            order_type=_required_text(row, "orderType"),
            exec_type=exec_type,
            exec_time_ms=_required_int(row, "execTime"),
            exec_price=_required_decimal(row, "execPrice"),
            exec_qty=_required_decimal(row, "execQty"),
            exec_value=_required_decimal(row, "execValue"),
            exec_fee=_required_decimal(row, "execFee"),
            fee_currency=_optional_upper_text(row, "feeCurrency"),
            fee_rate=_required_decimal(row, "feeRate"),
            is_maker=_required_bool(row, "isMaker"),
            leaves_qty=_required_decimal(row, "leavesQty"),
            closed_size=_optional_decimal(row, "closedSize"),
            seq=_required_int(row, "seq"),
        )
        item.validate(window=window)
        previous = records.get(item.exec_id)
        if previous is not None and previous != item:
            raise BybitMainnetActivityError(
                "Bybit execution ID returned conflicting broker records"
            )
        records[item.exec_id] = item
    ordered = tuple(
        sorted(
            records.values(),
            key=lambda item: (
                item.exec_time_ms,
                item.exec_id,
                item.order_id,
                item.leaves_qty,
            ),
        )
    )
    return ordered, excluded_non_trade


def _parse_closed_pnl(
    rows: tuple[Mapping[str, Any], ...],
    *,
    window: BybitMainnetActivityWindow,
) -> tuple[tuple[BybitMainnetClosedPnlRecord, ...], int]:
    records: dict[tuple[str, str, int], BybitMainnetClosedPnlRecord] = {}
    excluded_non_usdt = 0
    for row in rows:
        symbol = _required_text(row, "symbol")
        if not symbol.endswith("USDT"):
            excluded_non_usdt += 1
            continue
        item = BybitMainnetClosedPnlRecord(
            symbol=symbol,
            order_id=_required_text(row, "orderId"),
            side=_required_text(row, "side"),
            order_type=_required_text(row, "orderType"),
            exec_type=_required_text(row, "execType"),
            qty=_required_decimal(row, "qty"),
            closed_size=_required_decimal(row, "closedSize"),
            cumulative_entry_value=_required_decimal(row, "cumEntryValue"),
            average_entry_price=_required_decimal(row, "avgEntryPrice"),
            cumulative_exit_value=_required_decimal(row, "cumExitValue"),
            average_exit_price=_required_decimal(row, "avgExitPrice"),
            closed_pnl=_required_decimal(row, "closedPnl"),
            fill_count=_required_int(row, "fillCount"),
            leverage=_required_decimal(row, "leverage"),
            open_fee=_optional_decimal(row, "openFee"),
            close_fee=_optional_decimal(row, "closeFee"),
            created_time_ms=_required_int(row, "createdTime"),
            updated_time_ms=_required_int(row, "updatedTime"),
        )
        item.validate(window=window)
        identity = (item.symbol, item.order_id, item.updated_time_ms)
        previous = records.get(identity)
        if previous is not None and previous != item:
            raise BybitMainnetActivityError(
                "Bybit closed-PnL identity returned conflicting broker records"
            )
        records[identity] = item
    ordered = tuple(
        sorted(
            records.values(),
            key=lambda item: (item.updated_time_ms, item.symbol, item.order_id),
        )
    )
    return ordered, excluded_non_usdt


def _parse_transactions(
    rows: tuple[Mapping[str, Any], ...],
    *,
    window: BybitMainnetActivityWindow,
) -> tuple[BybitMainnetTransactionRecord, ...]:
    records: dict[
        tuple[str, int, str | None, str | None, str],
        BybitMainnetTransactionRecord,
    ] = {}
    for row in rows:
        raw_side = _optional_text(row, "side")
        side = None if raw_side in {None, "", "None"} else raw_side
        item = BybitMainnetTransactionRecord(
            transaction_id=_required_text(row, "id"),
            transaction_time_ms=_required_int(row, "transactionTime"),
            transaction_type=_required_text(row, "type"),
            transaction_sub_type=_optional_text(row, "transSubType") or "",
            category=_required_text(row, "category"),
            currency=_required_text(row, "currency"),
            symbol=_optional_upper_text(row, "symbol"),
            side=side,
            trade_id=_optional_text(row, "tradeId"),
            order_id=_optional_text(row, "orderId"),
            order_link_id=_optional_text(row, "orderLinkId"),
            qty=_optional_decimal(row, "qty"),
            size=_optional_decimal(row, "size"),
            trade_price=_optional_decimal(row, "tradePrice"),
            funding=_blank_decimal_as_zero(row, "funding"),
            fee=_required_decimal(row, "fee"),
            cash_flow=_required_decimal(row, "cashFlow"),
            change=_required_decimal(row, "change"),
            cash_balance=_required_decimal(row, "cashBalance"),
            fee_rate=_optional_decimal(row, "feeRate"),
        )
        item.validate(window=window)
        previous = records.get(item.broker_identity)
        if previous is not None and previous != item:
            raise BybitMainnetActivityError(
                "Bybit transaction identity returned conflicting broker records"
            )
        records[item.broker_identity] = item
    return tuple(
        sorted(
            records.values(),
            key=lambda item: (
                item.transaction_time_ms,
                item.transaction_id,
                item.trade_id or "",
                item.order_id or "",
                item.transaction_type,
            ),
        )
    )


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value or value != value.strip():
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be non-empty normalized text",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return value


def _optional_text(row: Mapping[str, Any], field: str) -> str | None:
    value = row.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value != value.strip():
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be normalized text when present",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return value


def _optional_upper_text(row: Mapping[str, Any], field: str) -> str | None:
    value = _optional_text(row, field)
    if value is None:
        return None
    if value != value.upper() or not value.isalnum():
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be normalized uppercase text",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return value


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None or value == "":
        raise BybitRestProtocolError(
            f"Bybit activity field {field} is required",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return _parse_decimal(value, field=field)


def _optional_decimal(row: Mapping[str, Any], field: str) -> Decimal | None:
    value = row.get(field)
    if value is None or value == "":
        return None
    return _parse_decimal(value, field=field)


def _blank_decimal_as_zero(row: Mapping[str, Any], field: str) -> Decimal:
    if field not in row:
        raise BybitRestProtocolError(
            f"Bybit activity field {field} is required",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    value = row[field]
    if value == "":
        return _ZERO
    return _parse_decimal(value, field=field)


def _parse_decimal(value: Any, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be decimal",
            retryable_read=False,
            ambiguous_mutation=False,
        ) from exc
    if not parsed.is_finite():
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be finite",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return parsed


def _required_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be integer",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be integer",
            retryable_read=False,
            ambiguous_mutation=False,
        ) from exc
    return parsed


def _required_bool(row: Mapping[str, Any], field: str) -> bool:
    value = row.get(field)
    if not isinstance(value, bool):
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be boolean",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return value


def _validate_required_text(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"Bybit activity {name} must be non-empty normalized text")


def _validate_usdt_symbol(symbol: str) -> None:
    if (
        not isinstance(symbol, str)
        or symbol != symbol.strip().upper()
        or not symbol.endswith("USDT")
        or not symbol[:-4].isalnum()
    ):
        raise ValueError("Bybit activity symbol must be normalized USDT symbol")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _execution_safe_dict(item: BybitMainnetExecutionRecord) -> dict[str, object]:
    return {
        "symbol": item.symbol,
        "exec_id": item.exec_id,
        "order_id": item.order_id,
        "order_link_id": item.order_link_id,
        "side": item.side,
        "order_type": item.order_type,
        "exec_type": item.exec_type,
        "exec_time_ms": item.exec_time_ms,
        "exec_price": _decimal_text(item.exec_price),
        "exec_qty": _decimal_text(item.exec_qty),
        "exec_value": _decimal_text(item.exec_value),
        "exec_fee": _decimal_text(item.exec_fee),
        "fee_currency": item.fee_currency,
        "fee_rate": _decimal_text(item.fee_rate),
        "is_maker": item.is_maker,
        "leaves_qty": _decimal_text(item.leaves_qty),
        "closed_size": _optional_decimal_text(item.closed_size),
        "seq": item.seq,
    }


def _closed_pnl_safe_dict(item: BybitMainnetClosedPnlRecord) -> dict[str, object]:
    return {
        "symbol": item.symbol,
        "order_id": item.order_id,
        "side": item.side,
        "order_type": item.order_type,
        "exec_type": item.exec_type,
        "qty": _decimal_text(item.qty),
        "closed_size": _decimal_text(item.closed_size),
        "cumulative_entry_value": _decimal_text(item.cumulative_entry_value),
        "average_entry_price": _decimal_text(item.average_entry_price),
        "cumulative_exit_value": _decimal_text(item.cumulative_exit_value),
        "average_exit_price": _decimal_text(item.average_exit_price),
        "closed_pnl": _decimal_text(item.closed_pnl),
        "fill_count": item.fill_count,
        "leverage": _decimal_text(item.leverage),
        "open_fee": _optional_decimal_text(item.open_fee),
        "close_fee": _optional_decimal_text(item.close_fee),
        "created_time_ms": item.created_time_ms,
        "updated_time_ms": item.updated_time_ms,
    }


def _transaction_safe_dict(item: BybitMainnetTransactionRecord) -> dict[str, object]:
    return {
        "transaction_id": item.transaction_id,
        "transaction_time_ms": item.transaction_time_ms,
        "transaction_type": item.transaction_type,
        "transaction_sub_type": item.transaction_sub_type,
        "category": item.category,
        "currency": item.currency,
        "symbol": item.symbol,
        "side": item.side,
        "trade_id": item.trade_id,
        "order_id": item.order_id,
        "order_link_id": item.order_link_id,
        "qty": _optional_decimal_text(item.qty),
        "size": _optional_decimal_text(item.size),
        "trade_price": _optional_decimal_text(item.trade_price),
        "funding": _decimal_text(item.funding),
        "fee": _decimal_text(item.fee),
        "cash_flow": _decimal_text(item.cash_flow),
        "change": _decimal_text(item.change),
        "cash_balance": _decimal_text(item.cash_balance),
        "fee_rate": _optional_decimal_text(item.fee_rate),
    }
