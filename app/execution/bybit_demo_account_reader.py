from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlencode

_DEMO_HOST = "api-demo.bybit.com"
_RECV_WINDOW_MS = 5000
_TRANSACTION_LOG_MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
_ALLOWED_MARGIN_MODES = {"ISOLATED_MARGIN", "REGULAR_MARGIN", "PORTFOLIO_MARGIN"}


class BybitDemoAccountingTransport(Protocol):
    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class BybitDemoAccountingPage:
    rows: tuple[Mapping[str, Any], ...]
    next_page_cursor: str | None


@dataclass(frozen=True)
class BybitDemoWalletBalance:
    total_equity_usd: Decimal
    total_wallet_balance_usd: Decimal
    total_margin_balance_usd: Decimal
    total_available_balance_usd: Decimal
    total_perp_upl_usd: Decimal
    total_initial_margin_usd: Decimal
    total_maintenance_margin_usd: Decimal

    def validate(self) -> None:
        values = (
            self.total_equity_usd,
            self.total_wallet_balance_usd,
            self.total_margin_balance_usd,
            self.total_available_balance_usd,
            self.total_perp_upl_usd,
            self.total_initial_margin_usd,
            self.total_maintenance_margin_usd,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("Bybit demo wallet balance fields must be finite")
        if self.total_equity_usd <= 0:
            raise ValueError("Bybit demo wallet total equity must be positive")
        if self.total_initial_margin_usd < 0 or self.total_maintenance_margin_usd < 0:
            raise ValueError("Bybit demo wallet margin requirements cannot be negative")


@dataclass(frozen=True)
class BybitDemoAccountInfo:
    margin_mode: str
    unified_margin_status: int
    updated_time_ms: int

    def validate(self) -> None:
        if self.margin_mode not in _ALLOWED_MARGIN_MODES:
            raise ValueError("Bybit demo account returned unsupported margin mode")
        if self.unified_margin_status <= 0:
            raise ValueError("Bybit demo unified margin status must be positive")
        if self.updated_time_ms < 0:
            raise ValueError("Bybit demo account updated time cannot be negative")


class BybitDemoHttpsAccountingTransport:
    """Exact-host HTTPS GET transport. It exposes no mutation method."""

    host = _DEMO_HOST
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        if not path.startswith("/v5/") or "?" in path or "#" in path:
            raise ValueError("Bybit demo accounting path must be a plain V5 path")
        target = path if not query_string else f"{path}?{query_string}"
        connection = http.client.HTTPSConnection(_DEMO_HOST, 443, timeout=10)
        try:
            connection.request("GET", target, headers=dict(headers))
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        if response.status != 200:
            raise RuntimeError(f"Bybit demo accounting HTTP status {response.status}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Bybit demo accounting returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Bybit demo accounting response must be an object")
        return payload


class BybitDemoAccountingClient:
    """Authenticated demo-only V5 accounting reads with no order-routing surface."""

    host = _DEMO_HOST
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        transport: BybitDemoAccountingTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
        recv_window_ms: int = _RECV_WINDOW_MS,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("Bybit demo accounting credentials cannot be empty")
        if not 1000 <= recv_window_ms <= 10000:
            raise ValueError("Bybit demo accounting recv window must be within [1000, 10000]")
        self._api_key = api_key
        self._api_secret = api_secret
        self._transport = (
            BybitDemoHttpsAccountingTransport() if transport is None else transport
        )
        self._clock_ms = (
            (lambda: int(time.time() * 1000)) if clock_ms is None else clock_ms
        )
        self._recv_window_ms = recv_window_ms

    def get_wallet_balance(self) -> BybitDemoWalletBalance:
        page = self._private_get_page(
            path="/v5/account/wallet-balance",
            query={"accountType": "UNIFIED"},
        )
        if page.next_page_cursor is not None:
            raise RuntimeError("Bybit demo wallet balance unexpectedly returned a cursor")
        if len(page.rows) != 1:
            raise RuntimeError("Bybit demo wallet balance must return exactly one account row")
        row = page.rows[0]
        if row.get("accountType") != "UNIFIED":
            raise RuntimeError("Bybit demo wallet balance returned a non-UNIFIED account")
        balance = BybitDemoWalletBalance(
            total_equity_usd=_wallet_decimal(row, "totalEquity"),
            total_wallet_balance_usd=_wallet_decimal(row, "totalWalletBalance"),
            total_margin_balance_usd=_wallet_decimal(row, "totalMarginBalance"),
            total_available_balance_usd=_wallet_decimal(row, "totalAvailableBalance"),
            total_perp_upl_usd=_wallet_decimal(row, "totalPerpUPL"),
            total_initial_margin_usd=_wallet_decimal(row, "totalInitialMargin"),
            total_maintenance_margin_usd=_wallet_decimal(row, "totalMaintenanceMargin"),
        )
        balance.validate()
        return balance

    def get_account_info(self) -> BybitDemoAccountInfo:
        result = self._private_get_result(path="/v5/account/info", query={})
        margin_mode = result.get("marginMode")
        if not isinstance(margin_mode, str):
            raise ValueError("Bybit demo account info is missing marginMode")
        info = BybitDemoAccountInfo(
            margin_mode=margin_mode,
            unified_margin_status=_required_int(result, "unifiedMarginStatus"),
            updated_time_ms=_required_int(result, "updatedTime"),
        )
        info.validate()
        return info

    def get_closed_pnl(
        self,
        *,
        symbol: str,
        limit: int = 100,
        max_pages: int = 10,
    ) -> tuple[Mapping[str, Any], ...]:
        _validate_symbol(symbol)
        return self._paginate(
            path="/v5/position/closed-pnl",
            base_query={
                "category": "linear",
                "symbol": symbol,
                "limit": str(_validate_limit(limit, maximum=100)),
            },
            max_pages=max_pages,
        )

    def get_transaction_log(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 50,
        max_pages: int = 20,
        transaction_type: str | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Read exact-symbol demo transaction logs through documented V5 filters.

        V5 transaction-log does not expose a request-side ``symbol`` filter. We query the
        documented UNIFIED/linear/USDT/baseCoin surface, then retain only the requested symbol.
        Requests longer than seven days are split into non-overlapping API-valid windows.
        """

        _validate_symbol(symbol)
        _validate_time_range(start_time_ms, end_time_ms)
        validated_limit = _validate_limit(limit, maximum=50)
        validated_type = _validate_transaction_type(transaction_type)
        base_coin = symbol[: -len("USDT")]

        rows: list[Mapping[str, Any]] = []
        window_start = start_time_ms
        while True:
            window_end = min(
                end_time_ms,
                window_start + _TRANSACTION_LOG_MAX_WINDOW_MS,
            )
            query = {
                "accountType": "UNIFIED",
                "category": "linear",
                "currency": "USDT",
                "baseCoin": base_coin,
                "startTime": str(window_start),
                "endTime": str(window_end),
                "limit": str(validated_limit),
            }
            if validated_type is not None:
                query["type"] = validated_type
            window_rows = self._paginate(
                path="/v5/account/transaction-log",
                base_query=query,
                max_pages=max_pages,
            )
            rows.extend(row for row in window_rows if row.get("symbol") == symbol)
            if window_end >= end_time_ms:
                break
            window_start = window_end + 1
        return tuple(rows)

    def _paginate(
        self,
        *,
        path: str,
        base_query: Mapping[str, str],
        max_pages: int,
    ) -> tuple[Mapping[str, Any], ...]:
        if not 1 <= max_pages <= 100:
            raise ValueError("Bybit demo accounting max_pages must be within [1, 100]")
        rows: list[Mapping[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(max_pages):
            query = dict(base_query)
            if cursor:
                query["cursor"] = cursor
            page = self._private_get_page(path=path, query=query)
            rows.extend(page.rows)
            if not page.next_page_cursor:
                return tuple(rows)
            if page.next_page_cursor in seen_cursors:
                raise RuntimeError("Bybit demo accounting cursor loop detected")
            seen_cursors.add(page.next_page_cursor)
            cursor = page.next_page_cursor
        raise RuntimeError("Bybit demo accounting pagination exceeded max_pages")

    def _private_get_page(
        self,
        *,
        path: str,
        query: Mapping[str, str],
    ) -> BybitDemoAccountingPage:
        result = self._private_get_result(path=path, query=query)
        raw_rows = result.get("list")
        if not isinstance(raw_rows, list):
            raise RuntimeError("Bybit demo accounting result.list must be an array")
        rows: list[Mapping[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, Mapping):
                raise RuntimeError("Bybit demo accounting row must be an object")
            rows.append(dict(row))
        raw_cursor = result.get("nextPageCursor")
        cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
        return BybitDemoAccountingPage(tuple(rows), cursor)

    def _private_get_result(
        self,
        *,
        path: str,
        query: Mapping[str, str],
    ) -> Mapping[str, Any]:
        query_string = urlencode(sorted(query.items()))
        timestamp = str(self._clock_ms())
        recv_window = str(self._recv_window_ms)
        signature_payload = timestamp + self._api_key + recv_window + query_string
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }
        payload = self._transport.get(
            path=path,
            query_string=query_string,
            headers=headers,
        )
        ret_code = payload.get("retCode")
        if ret_code != 0:
            raise RuntimeError(f"Bybit demo accounting retCode {ret_code}")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise RuntimeError("Bybit demo accounting result must be an object")
        return result


def _wallet_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    raw = row.get(field)
    if raw in (None, ""):
        raise ValueError(f"Bybit demo wallet balance is missing {field}")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Bybit demo wallet balance has invalid {field}") from exc
    if not value.is_finite():
        raise ValueError(f"Bybit demo wallet balance has non-finite {field}")
    return value


def _required_int(row: Mapping[str, Any], field: str) -> int:
    raw = row.get(field)
    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"Bybit demo account info is missing {field}")
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit demo account info has invalid {field}") from exc
    return value


def _validate_symbol(symbol: str) -> None:
    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise ValueError("Bybit demo accounting symbol must be normalized USDT symbol")
    if not symbol.replace("USDT", "", 1).isalnum():
        raise ValueError("Bybit demo accounting symbol contains invalid characters")


def _validate_time_range(start_time_ms: int, end_time_ms: int) -> None:
    if (
        isinstance(start_time_ms, bool)
        or isinstance(end_time_ms, bool)
        or start_time_ms < 0
        or end_time_ms < 0
        or end_time_ms < start_time_ms
    ):
        raise ValueError("Bybit transaction-log time range is invalid")


def _validate_limit(limit: int, *, maximum: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ValueError(
            f"Bybit demo accounting limit must be within [1, {maximum}]"
        )
    return limit


def _validate_transaction_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if not normalized or normalized != value or not normalized.replace("_", "").isalnum():
        raise ValueError("Bybit transaction-log type must be normalized uppercase enum text")
    return normalized
