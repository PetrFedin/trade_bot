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

_MAX_ACTIVITY_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
_DEFAULT_ACTIVITY_WINDOW_MS = 24 * 60 * 60 * 1000
_ZERO = Decimal("0")


class BybitMainnetActivityError(RuntimeError):
    """Raised when real-account read-only activity cannot be represented safely."""


@dataclass(frozen=True)
class BybitMainnetActivityWindow:
    start_time_ms: int
    end_time_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_time_ms - self.start_time_ms

    def validate(self) -> None:
        for field_name, value in (
            ("start_time_ms", self.start_time_ms),
            ("end_time_ms", self.end_time_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"Bybit mainnet activity {field_name} must be non-negative integer ms"
                )
        if self.end_time_ms <= self.start_time_ms:
            raise ValueError("Bybit mainnet activity window must have positive duration")
        if self.duration_ms > _MAX_ACTIVITY_WINDOW_MS:
            raise ValueError("Bybit mainnet activity window cannot exceed 7 days")

    @classmethod
    def last_24_hours_ending_at(cls, end_time_ms: int) -> BybitMainnetActivityWindow:
        if (
            isinstance(end_time_ms, bool)
            or not isinstance(end_time_ms, int)
            or end_time_ms < _DEFAULT_ACTIVITY_WINDOW_MS
        ):
            raise ValueError("Bybit mainnet activity end time cannot form a 24-hour window")
        window = cls(
            start_time_ms=end_time_ms - _DEFAULT_ACTIVITY_WINDOW_MS,
            end_time_ms=end_time_ms,
        )
        window.validate()
        return window


@dataclass(frozen=True)
class BybitMainnetExecution:
    symbol: str
    order_id: str
    order_link_id: str
    side: str
    order_type: str
    leaves_qty: Decimal
    exec_fee: Decimal
    exec_id: str
    exec_price: Decimal
    exec_qty: Decimal
    exec_type: str
    exec_value: Decimal
    exec_time_ms: int
    fee_currency: str | None
    is_maker: bool
    fee_rate: Decimal
    closed_size: Decimal | None
    seq: int

    def validate(self, *, window: BybitMainnetActivityWindow) -> None:
        window.validate()
        _validate_usdt_symbol(self.symbol)
        _validate_required_text(self.order_id, name="execution order_id")
        _validate_required_text(self.exec_id, name="execution exec_id")
        if self.order_link_id != self.order_link_id.strip():
            raise ValueError("Bybit execution order_link_id cannot contain surrounding whitespace")
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("Bybit execution side must be Buy or Sell")
        _validate_required_text(self.order_type, name="execution order_type")
        _validate_required_text(self.exec_type, name="execution exec_type")
        for name, value in (
            ("leaves_qty", self.leaves_qty),
            ("exec_fee", self.exec_fee),
            ("exec_price", self.exec_price),
            ("exec_qty", self.exec_qty),
            ("exec_value", self.exec_value),
            ("fee_rate", self.fee_rate),
        ):
            if not value.is_finite():
                raise ValueError(f"Bybit execution {name} must be finite")
        if self.leaves_qty < _ZERO:
            raise ValueError("Bybit execution leaves_qty cannot be negative")
        if self.exec_price <= _ZERO or self.exec_qty <= _ZERO or self.exec_value <= _ZERO:
            raise ValueError("Bybit execution price/qty/value must be positive")
        if self.closed_size is not None and (
            not self.closed_size.is_finite() or self.closed_size < _ZERO
        ):
            raise ValueError("Bybit execution closed_size must be non-negative and finite")
        if self.fee_currency is not None:
            if (
                self.fee_currency != self.fee_currency.strip().upper()
                or not self.fee_currency
                or not self.fee_currency.isalnum()
            ):
                raise ValueError("Bybit execution fee_currency must be normalized uppercase text")
        if not isinstance(self.is_maker, bool):
            raise ValueError("Bybit execution is_maker must be boolean")
        if (
            isinstance(self.exec_time_ms, bool)
            or not isinstance(self.exec_time_ms, int)
            or not window.start_time_ms <= self.exec_time_ms <= window.end_time_ms
        ):
            raise ValueError("Bybit execution exec_time_ms is outside the requested window")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise ValueError("Bybit execution seq must be non-negative integer")


@dataclass(frozen=True)
class BybitMainnetClosedPnl:
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
        _validate_required_text(self.order_id, name="closed PnL order_id")
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("Bybit closed PnL side must be Buy or Sell")
        _validate_required_text(self.order_type, name="closed PnL order_type")
        _validate_required_text(self.exec_type, name="closed PnL exec_type")
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
            raise ValueError("Bybit closed PnL qty/closed_size must be positive")
        if (
            self.cumulative_entry_value < _ZERO
            or self.average_entry_price <= _ZERO
            or self.cumulative_exit_value < _ZERO
            or self.average_exit_price <= _ZERO
            or self.leverage <= _ZERO
        ):
            raise ValueError("Bybit closed PnL price/value/leverage economics are invalid")
        for name, value in (("open_fee", self.open_fee), ("close_fee", self.close_fee)):
            if value is not None and not value.is_finite():
                raise ValueError(f"Bybit closed PnL {name} must be finite")
        if isinstance(self.fill_count, bool) or not isinstance(self.fill_count, int):
            raise ValueError("Bybit closed PnL fill_count must be integer")
        if self.fill_count < 0:
            raise ValueError("Bybit closed PnL fill_count cannot be negative")
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
class BybitMainnetTransaction:
    transaction_id: str
    symbol: str | None
    category: str
    side: str | None
    transaction_time_ms: int
    transaction_type: str
    transaction_sub_type: str
    qty: Decimal | None
    size: Decimal | None
    currency: str
    trade_price: Decimal | None
    funding: Decimal
    fee: Decimal
    cash_flow: Decimal
    change: Decimal
    cash_balance: Decimal
    fee_rate: Decimal | None
    trade_id: str | None
    order_id: str | None
    order_link_id: str | None

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
        _validate_required_text(self.transaction_id, name="transaction id")
        if self.symbol is not None:
            _validate_usdt_symbol(self.symbol)
        if self.category != "linear":
            raise ValueError("Bybit mainnet transaction category must be linear")
        if self.side not in {None, "Buy", "Sell"}:
            raise ValueError("Bybit mainnet transaction side must be Buy, Sell or None")
        if (
            isinstance(self.transaction_time_ms, bool)
            or not isinstance(self.transaction_time_ms, int)
            or not window.start_time_ms <= self.transaction_time_ms <= window.end_time_ms
        ):
            raise ValueError("Bybit transaction time is outside the requested window")
        _validate_required_text(self.transaction_type, name="transaction type")
        if self.transaction_sub_type != self.transaction_sub_type.strip():
            raise ValueError("Bybit transaction sub-type cannot contain surrounding whitespace")
        if self.currency != "USDT":
            raise ValueError("Bybit mainnet transaction currency must be USDT")
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
        expected_change = self.cash_flow + self.funding - self.fee
        if self.change != expected_change:
            raise BybitMainnetActivityError(
                "Bybit transaction violates change = cashFlow + funding - fee"
            )
        for name, value in (
            ("trade_id", self.trade_id),
            ("order_id", self.order_id),
            ("order_link_id", self.order_link_id),
        ):
            if value is not None and (not value or value != value.strip()):
                raise ValueError(f"Bybit transaction {name} must be normalized when present")


@dataclass(frozen=True)
class BybitMainnetActivitySnapshot:
    window: BybitMainnetActivityWindow
    api_host: str
    api_key_fingerprint_sha256: str
    executions: tuple[BybitMainnetExecution, ...]
    closed_pnl: tuple[BybitMainnetClosedPnl, ...]
    transactions: tuple[BybitMainnetTransaction, ...]
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
        if (
            isinstance(self.excluded_non_usdt_closed_pnl_count, bool)
            or not isinstance(self.excluded_non_usdt_closed_pnl_count, int)
            or self.excluded_non_usdt_closed_pnl_count < 0
        ):
            raise ValueError("Bybit activity excluded closed-PnL count must be non-negative")
        totals = (
            self.transaction_cash_flow_usdt,
            self.transaction_funding_usdt,
            self.transaction_fee_usdt,
            self.transaction_change_usdt,
        )
        if any(not value.is_finite() for value in totals):
            raise ValueError("Bybit activity transaction totals must be finite")
        expected_cash_flow = sum(
            (record.cash_flow for record in self.transactions), start=_ZERO
        )
        expected_funding = sum((record.funding for record in self.transactions), start=_ZERO)
        expected_fee = sum((record.fee for record in self.transactions), start=_ZERO)
        expected_change = sum((record.change for record in self.transactions), start=_ZERO)
        if (
            self.transaction_cash_flow_usdt != expected_cash_flow
            or self.transaction_funding_usdt != expected_funding
            or self.transaction_fee_usdt != expected_fee
            or self.transaction_change_usdt != expected_change
        ):
            raise ValueError("Bybit activity transaction totals do not match records")
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
                "excluded_non_usdt_closed_pnl_count": self.excluded_non_usdt_closed_pnl_count,
                "transaction_cash_flow_usdt": _decimal_text(
                    self.transaction_cash_flow_usdt
                ),
                "transaction_funding_usdt": _decimal_text(self.transaction_funding_usdt),
                "transaction_fee_usdt": _decimal_text(self.transaction_fee_usdt),
                "transaction_change_usdt": _decimal_text(self.transaction_change_usdt),
            },
            "executions": [_execution_safe_dict(record) for record in self.executions],
            "closed_pnl": [_closed_pnl_safe_dict(record) for record in self.closed_pnl],
            "transactions": [_transaction_safe_dict(record) for record in self.transactions],
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
    """Read and type one bounded real-account activity window after key safety proof."""

    if client.live_mainnet_order_routing_allowed or client.order_writes_supported:
        raise BybitMainnetReadOnlyError("Bybit activity reader rejected a mutation-capable client")
    window.validate()
    key_info = client.verify_read_only_api_key(require_ip_binding=True)
    execution_rows = client.read_execution_rows(window)
    closed_rows = client.read_closed_pnl_rows(window)
    transaction_rows = client.read_transaction_rows(window)

    executions = _dedupe_and_sort_executions(execution_rows, window=window)
    closed_pnl, excluded_non_usdt = _dedupe_and_sort_closed_pnl(
        closed_rows,
        window=window,
    )
    transactions = _dedupe_and_sort_transactions(transaction_rows, window=window)

    cash_flow = sum((record.cash_flow for record in transactions), start=_ZERO)
    funding = sum((record.funding for record in transactions), start=_ZERO)
    fee = sum((record.fee for record in transactions), start=_ZERO)
    change = sum((record.change for record in transactions), start=_ZERO)
    snapshot = BybitMainnetActivitySnapshot(
        window=window,
        api_host=client.host,
        api_key_fingerprint_sha256=key_info.key_fingerprint_sha256,
        executions=executions,
        closed_pnl=closed_pnl,
        transactions=transactions,
        excluded_non_usdt_closed_pnl_count=excluded_non_usdt,
        transaction_cash_flow_usdt=cash_flow,
        transaction_funding_usdt=funding,
        transaction_fee_usdt=fee,
        transaction_change_usdt=change,
    )
    snapshot.validate()
    return snapshot


def _dedupe_and_sort_executions(
    rows: tuple[Mapping[str, Any], ...],
    *,
    window: BybitMainnetActivityWindow,
) -> tuple[BybitMainnetExecution, ...]:
    by_id: dict[str, BybitMainnetExecution] = {}
    for row in rows:
        record = _parse_execution(row)
        record.validate(window=window)
        existing = by_id.get(record.exec_id)
        if existing is not None and existing != record:
            raise BybitMainnetActivityError(
                "Bybit execution ID returned conflicting broker records"
            )
        by_id[record.exec_id] = record
    return tuple(
        sorted(
            by_id.values(),
            key=lambda item: (
                item.exec_time_ms,
                item.exec_id,
                item.order_id,
                item.leaves_qty,
            ),
        )
    )


def _dedupe_and_sort_closed_pnl(
    rows: tuple[Mapping[str, Any], ...],
    *,
    window: BybitMainnetActivityWindow,
) -> tuple[tuple[BybitMainnetClosedPnl, ...], int]:
    excluded_non_usdt = 0
    records: dict[tuple[str, str, int], BybitMainnetClosedPnl] = {}
    for row in rows:
        raw_symbol = row.get("symbol")
        if not isinstance(raw_symbol, str):
            raise BybitRestProtocolError(
                "Bybit closed PnL row is missing symbol",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        if not raw_symbol.endswith("USDT"):
            excluded_non_usdt += 1
            continue
        record = _parse_closed_pnl(row)
        record.validate(window=window)
        identity = (record.symbol, record.order_id, record.updated_time_ms)
        existing = records.get(identity)
        if existing is not None and existing != record:
            raise BybitMainnetActivityError(
                "Bybit closed-PnL identity returned conflicting broker records"
            )
        records[identity] = record
    return (
        tuple(
            sorted(
                records.values(),
                key=lambda item: (item.updated_time_ms, item.symbol, item.order_id),
            )
        ),
        excluded_non_usdt,
    )


def _dedupe_and_sort_transactions(
    rows: tuple[Mapping[str, Any], ...],
    *,
    window: BybitMainnetActivityWindow,
) -> tuple[BybitMainnetTransaction, ...]:
    records: dict[
        tuple[str, int, str | None, str | None, str],
        BybitMainnetTransaction,
    ] = {}
    for row in rows:
        record = _parse_transaction(row)
        record.validate(window=window)
        existing = records.get(record.broker_identity)
        if existing is not None and existing != record:
            raise BybitMainnetActivityError(
                "Bybit transaction identity returned conflicting broker records"
            )
        records[record.broker_identity] = record
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


def _parse_execution(row: Mapping[str, Any]) -> BybitMainnetExecution:
    return BybitMainnetExecution(
        symbol=_required_text(row, "symbol"),
        order_id=_required_text(row, "orderId"),
        order_link_id=_optional_text(row, "orderLinkId") or "",
        side=_required_text(row, "side"),
        order_type=_required_text(row, "orderType"),
        leaves_qty=_required_decimal(row, "leavesQty"),
        exec_fee=_required_decimal(row, "execFee"),
        exec_id=_required_text(row, "execId"),
        exec_price=_required_decimal(row, "execPrice"),
        exec_qty=_required_decimal(row, "execQty"),
        exec_type=_required_text(row, "execType"),
        exec_value=_required_decimal(row, "execValue"),
        exec_time_ms=_required_int(row, "execTime"),
        fee_currency=_optional_normalized_upper_text(row, "feeCurrency"),
        is_maker=_required_bool(row, "isMaker"),
        fee_rate=_required_decimal(row, "feeRate"),
        closed_size=_optional_decimal(row, "closedSize"),
        seq=_required_int(row, "seq"),
    )


def _parse_closed_pnl(row: Mapping[str, Any]) -> BybitMainnetClosedPnl:
    return BybitMainnetClosedPnl(
        symbol=_required_text(row, "symbol"),
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


def _parse_transaction(row: Mapping[str, Any]) -> BybitMainnetTransaction:
    raw_side = _optional_text(row, "side")
    side = None if raw_side in {None, "None", ""} else raw_side
    return BybitMainnetTransaction(
        transaction_id=_required_text(row, "id"),
        symbol=_optional_normalized_upper_text(row, "symbol"),
        category=_required_text(row, "category"),
        side=side,
        transaction_time_ms=_required_int(row, "transactionTime"),
        transaction_type=_required_text(row, "type"),
        transaction_sub_type=_optional_text(row, "transSubType") or "",
        qty=_optional_decimal(row, "qty"),
        size=_optional_decimal(row, "size"),
        currency=_required_text(row, "currency"),
        trade_price=_optional_decimal(row, "tradePrice"),
        funding=_blank_decimal_as_zero(row, "funding"),
        fee=_required_decimal(row, "fee"),
        cash_flow=_required_decimal(row, "cashFlow"),
        change=_required_decimal(row, "change"),
        cash_balance=_required_decimal(row, "cashBalance"),
        fee_rate=_optional_decimal(row, "feeRate"),
        trade_id=_optional_text(row, "tradeId"),
        order_id=_optional_text(row, "orderId"),
        order_link_id=_optional_text(row, "orderLinkId"),
    )


def _required_text(row: Mapping[str, Any], field: str) -> str:
    raw = row.get(field)
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be non-empty normalized text",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return raw


def _optional_text(row: Mapping[str, Any], field: str) -> str | None:
    raw = row.get(field)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or raw != raw.strip():
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be normalized text when present",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return raw


def _optional_normalized_upper_text(
    row: Mapping[str, Any],
    field: str,
) -> str | None:
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
    raw = row.get(field)
    if raw is None or raw == "":
        raise BybitRestProtocolError(
            f"Bybit activity field {field} is required",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return _parse_decimal(raw, field=field)


def _optional_decimal(row: Mapping[str, Any], field: str) -> Decimal | None:
    raw = row.get(field)
    if raw is None or raw == "":
        return None
    return _parse_decimal(raw, field=field)


def _blank_decimal_as_zero(row: Mapping[str, Any], field: str) -> Decimal:
    raw = row.get(field)
    if raw is None:
        raise BybitRestProtocolError(
            f"Bybit activity field {field} is required",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    if raw == "":
        return _ZERO
    return _parse_decimal(raw, field=field)


def _parse_decimal(raw: Any, *, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be decimal",
            retryable_read=False,
            ambiguous_mutation=False,
        ) from exc
    if not value.is_finite():
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be finite",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return value


def _required_int(row: Mapping[str, Any], field: str) -> int:
    raw = row.get(field)
    if raw is None or isinstance(raw, bool):
        raise BybitRestProtocolError(
            f"Bybit activity field {field} is required integer",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be integer",
            retryable_read=False,
            ambiguous_mutation=False,
        ) from exc
    return value


def _required_bool(row: Mapping[str, Any], field: str) -> bool:
    raw = row.get(field)
    if not isinstance(raw, bool):
        raise BybitRestProtocolError(
            f"Bybit activity field {field} must be boolean",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return raw


def _validate_required_text(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"Bybit {name} must be non-empty normalized text")


def _validate_usdt_symbol(symbol: str) -> None:
    if (
        not isinstance(symbol, str)
        or symbol != symbol.strip().upper()
        or not symbol.endswith("USDT")
        or not symbol[:-4].isalnum()
    ):
        raise ValueError("Bybit mainnet activity symbol must be normalized USDT symbol")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _execution_safe_dict(record: BybitMainnetExecution) -> dict[str, object]:
    return {
        "symbol": record.symbol,
        "order_id": record.order_id,
        "order_link_id": record.order_link_id,
        "side": record.side,
        "order_type": record.order_type,
        "leaves_qty": _decimal_text(record.leaves_qty),
        "exec_fee": _decimal_text(record.exec_fee),
        "exec_id": record.exec_id,
        "exec_price": _decimal_text(record.exec_price),
        "exec_qty": _decimal_text(record.exec_qty),
        "exec_type": record.exec_type,
        "exec_value": _decimal_text(record.exec_value),
        "exec_time_ms": record.exec_time_ms,
        "fee_currency": record.fee_currency,
        "is_maker": record.is_maker,
        "fee_rate": _decimal_text(record.fee_rate),
        "closed_size": _optional_decimal_text(record.closed_size),
        "seq": record.seq,
    }


def _closed_pnl_safe_dict(record: BybitMainnetClosedPnl) -> dict[str, object]:
    return {
        "symbol": record.symbol,
        "order_id": record.order_id,
        "side": record.side,
        "order_type": record.order_type,
        "exec_type": record.exec_type,
        "qty": _decimal_text(record.qty),
        "closed_size": _decimal_text(record.closed_size),
        "cumulative_entry_value": _decimal_text(record.cumulative_entry_value),
        "average_entry_price": _decimal_text(record.average_entry_price),
        "cumulative_exit_value": _decimal_text(record.cumulative_exit_value),
        "average_exit_price": _decimal_text(record.average_exit_price),
        "closed_pnl": _decimal_text(record.closed_pnl),
        "fill_count": record.fill_count,
        "leverage": _decimal_text(record.leverage),
        "open_fee": _optional_decimal_text(record.open_fee),
        "close_fee": _optional_decimal_text(record.close_fee),
        "created_time_ms": record.created_time_ms,
        "updated_time_ms": record.updated_time_ms,
    }


def _transaction_safe_dict(record: BybitMainnetTransaction) -> dict[str, object]:
    return {
        "transaction_id": record.transaction_id,
        "symbol": record.symbol,
        "category": record.category,
        "side": record.side,
        "transaction_time_ms": record.transaction_time_ms,
        "transaction_type": record.transaction_type,
        "transaction_sub_type": record.transaction_sub_type,
        "qty": _optional_decimal_text(record.qty),
        "size": _optional_decimal_text(record.size),
        "currency": record.currency,
        "trade_price": _optional_decimal_text(record.trade_price),
        "funding": _decimal_text(record.funding),
        "fee": _decimal_text(record.fee),
        "cash_flow": _decimal_text(record.cash_flow),
        "change": _decimal_text(record.change),
        "cash_balance": _decimal_text(record.cash_balance),
        "fee_rate": _optional_decimal_text(record.fee_rate),
        "trade_id": record.trade_id,
        "order_id": record.order_id,
        "order_link_id": record.order_link_id,
    }
