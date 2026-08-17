from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

_DEMO_HOST = "api-demo.bybit.com"
_RECV_WINDOW_MS = 5000


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
                "limit": str(_validate_limit(limit)),
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
    ) -> tuple[Mapping[str, Any], ...]:
        _validate_symbol(symbol)
        if start_time_ms < 0 or end_time_ms < 0 or end_time_ms < start_time_ms:
            raise ValueError("Bybit transaction-log time range is invalid")
        return self._paginate(
            path="/v5/account/transaction-log",
            base_query={
                "category": "linear",
                "symbol": symbol,
                "startTime": str(start_time_ms),
                "endTime": str(end_time_ms),
                "limit": str(_validate_limit(limit)),
            },
            max_pages=max_pages,
        )

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
        query_string = urlencode(sorted(query.items()))
        timestamp = str(self._clock_ms())
        recv_window = str(self._recv_window_ms)
        signature_payload = (
            timestamp + self._api_key + recv_window + query_string
        )
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


def _validate_symbol(symbol: str) -> None:
    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise ValueError("Bybit demo accounting symbol must be normalized USDT symbol")
    if not symbol.replace("USDT", "", 1).isalnum():
        raise ValueError("Bybit demo accounting symbol contains invalid characters")


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("Bybit demo accounting limit must be within [1, 100]")
    return limit
