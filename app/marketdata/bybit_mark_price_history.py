from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode

from app.marketdata.bybit_http import decode_public_json_response
from app.marketdata.bybit_research_universe import validate_bybit_public_research_host

_MARK_PRICE_PATH = "/v5/market/mark-price-kline"
_ALLOWED_INTERVALS = frozenset({"1", "3", "5", "15", "30", "60", "120", "240"})
_MAX_LIMIT = 1000


@dataclass(frozen=True)
class BybitMarkPricePoint:
    symbol: str
    start_time_ms: int
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        _validate_timestamp(self.start_time_ms)
        for name, value in (
            ("open", self.open_price),
            ("high", self.high_price),
            ("low", self.low_price),
            ("close", self.close_price),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"Bybit mark-price {name} must be positive and finite")
        if self.high_price < max(self.open_price, self.close_price):
            raise ValueError("Bybit mark-price high is below open/close")
        if self.low_price > min(self.open_price, self.close_price):
            raise ValueError("Bybit mark-price low is above open/close")


@dataclass(frozen=True)
class BybitMarkPriceHistory:
    symbol: str
    start_ms: int
    end_ms: int
    interval: str
    points: tuple[BybitMarkPricePoint, ...]
    request_count: int
    host: str
    source: str = "BYBIT_V5_MARK_PRICE_KLINE"
    live_mainnet_order_routing_allowed: bool = False
    order_writes_supported: bool = False

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        validate_bybit_public_research_host(self.host)
        _validate_range(self.start_ms, self.end_ms)
        if self.interval not in _ALLOWED_INTERVALS:
            raise ValueError("Bybit mark-price interval is unsupported")
        if isinstance(self.request_count, bool) or self.request_count < 1:
            raise ValueError("Bybit mark-price request count must be positive")
        previous: int | None = None
        for point in self.points:
            point.validate()
            if point.symbol != self.symbol:
                raise ValueError("Bybit mark-price point symbol mismatch")
            if not self.start_ms <= point.start_time_ms <= self.end_ms:
                raise ValueError("Bybit mark-price point outside requested interval")
            if previous is not None and point.start_time_ms <= previous:
                raise ValueError("Bybit mark-price points must be strictly ordered")
            previous = point.start_time_ms
        if self.live_mainnet_order_routing_allowed or self.order_writes_supported:
            raise ValueError("Bybit mark-price history cannot grant order writes")

    def open_price_at(self, timestamp_ms: int) -> Decimal | None:
        _validate_timestamp(timestamp_ms)
        for point in self.points:
            if point.start_time_ms == timestamp_ms:
                return point.open_price
            if point.start_time_ms > timestamp_ms:
                break
        return None


@dataclass(frozen=True)
class BybitMarkPriceHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


Transport = Callable[[str, str, Mapping[str, str]], BybitMarkPriceHttpJson]


class BybitMarkPriceHistoryClient:
    """Public GET-only mark-price history used for funding settlement reconstruction."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(
        self,
        *,
        host: str = "api.bybit.com",
        transport: Transport | None = None,
        maximum_pages: int = 250,
    ) -> None:
        self.host = validate_bybit_public_research_host(host)
        if not 1 <= maximum_pages <= 1000:
            raise ValueError("Bybit mark-price pagination bound must be within [1, 1000]")
        self._transport = _https_transport if transport is None else transport
        self._maximum_pages = maximum_pages

    def fetch_history(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str = "60",
    ) -> BybitMarkPriceHistory:
        _validate_symbol(symbol)
        _validate_range(start_ms, end_ms)
        if interval not in _ALLOWED_INTERVALS:
            raise ValueError("Bybit mark-price interval is unsupported")
        interval_ms = _interval_ms(interval)
        cursor_end = end_ms
        request_count = 0
        by_time: dict[int, BybitMarkPricePoint] = {}
        while cursor_end >= start_ms:
            request_count += 1
            if request_count > self._maximum_pages:
                raise RuntimeError("Bybit mark-price pagination exceeded safety bound")
            payload = self._get(
                {
                    "category": "linear",
                    "symbol": symbol,
                    "interval": interval,
                    "start": str(start_ms),
                    "end": str(cursor_end),
                    "limit": str(_MAX_LIMIT),
                }
            )
            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise RuntimeError("Bybit mark-price result must be an object")
            if result.get("category") != "linear" or result.get("symbol") != symbol:
                raise RuntimeError("Bybit mark-price result identity mismatch")
            rows = result.get("list")
            if not isinstance(rows, list):
                raise RuntimeError("Bybit mark-price result.list must be an array")
            if not rows:
                break
            page_times: list[int] = []
            for raw in rows:
                point = _parse_point(symbol, raw)
                if not start_ms <= point.start_time_ms <= end_ms:
                    continue
                existing = by_time.get(point.start_time_ms)
                if existing is not None and existing != point:
                    raise RuntimeError("Bybit mark-price conflicting duplicate timestamp")
                by_time[point.start_time_ms] = point
                page_times.append(point.start_time_ms)
            if not page_times:
                break
            oldest = min(page_times)
            if oldest <= start_ms:
                break
            next_end = oldest - 1
            if next_end >= cursor_end:
                raise RuntimeError("Bybit mark-price pagination did not move backwards")
            cursor_end = next_end
            if len(rows) < _MAX_LIMIT:
                break
            if cursor_end < start_ms + interval_ms:
                break
        history = BybitMarkPriceHistory(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            interval=interval,
            points=tuple(by_time[key] for key in sorted(by_time)),
            request_count=request_count,
            host=self.host,
        )
        history.validate()
        return history

    def _get(self, query: Mapping[str, str]) -> Mapping[str, Any]:
        query_string = urlencode(sorted(query.items()))
        response = self._transport(self.host, _MARK_PRICE_PATH, {"query": query_string})
        if response.status_code != 200:
            raise RuntimeError(f"Bybit mark-price HTTP status {response.status_code}")
        if response.payload.get("retCode") != 0:
            raise RuntimeError(
                f"Bybit mark-price retCode {response.payload.get('retCode')}"
            )
        return response.payload


def _parse_point(symbol: str, raw: Any) -> BybitMarkPricePoint:
    if not isinstance(raw, list) or len(raw) < 5:
        raise RuntimeError("Bybit mark-price row must contain five values")
    point = BybitMarkPricePoint(
        symbol=symbol,
        start_time_ms=_required_int(raw[0], field="startTime"),
        open_price=_required_decimal(raw[1], field="openPrice"),
        high_price=_required_decimal(raw[2], field="highPrice"),
        low_price=_required_decimal(raw[3], field="lowPrice"),
        close_price=_required_decimal(raw[4], field="closePrice"),
    )
    point.validate()
    return point


def _https_transport(
    host: str,
    path: str,
    metadata: Mapping[str, str],
) -> BybitMarkPriceHttpJson:
    normalized_host = validate_bybit_public_research_host(host)
    if path != _MARK_PRICE_PATH:
        raise ValueError("Bybit mark-price transport rejected path")
    query_string = metadata.get("query", "")
    if not isinstance(query_string, str) or "#" in query_string:
        raise ValueError("Bybit mark-price transport rejected query")
    target = path if not query_string else f"{path}?{query_string}"
    connection = HTTPSConnection(normalized_host, 443, timeout=20)
    try:
        connection.request("GET", target, headers={"Accept": "application/json"})
        response = connection.getresponse()
        headers = {key: value for key, value in response.getheaders()}
        payload = decode_public_json_response(
            status_code=response.status,
            headers=headers,
            body=response.read(),
        )
        return BybitMarkPriceHttpJson(
            status_code=response.status,
            headers=headers,
            payload=payload,
        )
    finally:
        connection.close()


def _interval_ms(interval: str) -> int:
    if interval not in _ALLOWED_INTERVALS:
        raise ValueError("Bybit mark-price interval is unsupported")
    return int(interval) * 60_000


def _validate_symbol(symbol: str) -> None:
    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise ValueError("Bybit mark-price symbol must be normalized USDT")
    base = symbol[:-4]
    if not base or not base.isalnum():
        raise ValueError("Bybit mark-price symbol contains invalid characters")


def _validate_range(start_ms: int, end_ms: int) -> None:
    _validate_timestamp(start_ms)
    _validate_timestamp(end_ms)
    if end_ms <= start_ms:
        raise ValueError("Bybit mark-price end_ms must be after start_ms")


def _validate_timestamp(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Bybit mark-price timestamp must be non-negative integer ms")


def _required_int(value: Any, *, field: str) -> int:
    if value is None or isinstance(value, bool):
        raise RuntimeError(f"Bybit mark-price missing {field}")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Bybit mark-price invalid {field}") from exc
    if parsed < 0:
        raise RuntimeError(f"Bybit mark-price invalid {field}")
    return parsed


def _required_decimal(value: Any, *, field: str) -> Decimal:
    if value in (None, ""):
        raise RuntimeError(f"Bybit mark-price missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(f"Bybit mark-price invalid {field}") from exc
    if not parsed.is_finite():
        raise RuntimeError(f"Bybit mark-price invalid {field}")
    return parsed
