from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode

from app.marketdata.bybit_http import decode_public_json_response
from app.marketdata.bybit_research_universe import validate_bybit_public_research_host

_OPEN_INTEREST_PATH = "/v5/market/open-interest"
_ACCOUNT_RATIO_PATH = "/v5/market/account-ratio"
_FUNDING_PATH = "/v5/market/funding/history"
_ALLOWED_PATHS = frozenset({_OPEN_INTEREST_PATH, _ACCOUNT_RATIO_PATH, _FUNDING_PATH})
_ALLOWED_INTERVALS = frozenset({"5min", "15min", "30min", "1h", "4h", "1d"})
_MAX_OI_LIMIT = 200
_MAX_RATIO_LIMIT = 500
_MAX_FUNDING_LIMIT = 200
_ONE = Decimal("1")
_ZERO = Decimal("0")
_RATIO_SUM_TOLERANCE = Decimal("0.02")


@dataclass(frozen=True)
class BybitOpenInterestPoint:
    symbol: str
    timestamp_ms: int
    open_interest: Decimal
    single_open_interest: Decimal | None

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        _validate_timestamp(self.timestamp_ms)
        if not self.open_interest.is_finite() or self.open_interest < 0:
            raise ValueError("Bybit historical open interest must be finite and non-negative")
        if self.single_open_interest is not None:
            if not self.single_open_interest.is_finite() or self.single_open_interest < 0:
                raise ValueError(
                    "Bybit historical single open interest must be finite and non-negative"
                )


@dataclass(frozen=True)
class BybitAccountRatioPoint:
    symbol: str
    timestamp_ms: int
    buy_ratio: Decimal
    sell_ratio: Decimal

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        _validate_timestamp(self.timestamp_ms)
        for name, value in (("buy", self.buy_ratio), ("sell", self.sell_ratio)):
            if not value.is_finite() or not _ZERO <= value <= _ONE:
                raise ValueError(f"Bybit historical {name} ratio must be within [0, 1]")
        if abs(self.buy_ratio + self.sell_ratio - _ONE) > _RATIO_SUM_TOLERANCE:
            raise ValueError("Bybit historical long/short ratios do not reconcile to one")

    @property
    def long_short_ratio(self) -> Decimal | None:
        if self.sell_ratio == 0:
            return None
        return self.buy_ratio / self.sell_ratio


@dataclass(frozen=True)
class BybitHistoricalFundingPoint:
    symbol: str
    timestamp_ms: int
    funding_rate: Decimal

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        _validate_timestamp(self.timestamp_ms)
        if not self.funding_rate.is_finite():
            raise ValueError("Bybit historical funding rate must be finite")


@dataclass(frozen=True)
class BybitDerivativesHistory:
    symbol: str
    start_ms: int
    end_ms: int
    interval: str
    open_interest: tuple[BybitOpenInterestPoint, ...]
    account_ratio: tuple[BybitAccountRatioPoint, ...]
    funding: tuple[BybitHistoricalFundingPoint, ...]
    request_count: int
    host: str
    source: str = "BYBIT_V5_PUBLIC_DERIVATIVES_HISTORY"
    live_mainnet_order_routing_allowed: bool = False
    order_writes_supported: bool = False

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        validate_bybit_public_research_host(self.host)
        _validate_range(self.start_ms, self.end_ms)
        if self.interval not in _ALLOWED_INTERVALS:
            raise ValueError("Bybit derivatives history interval is unsupported")
        if isinstance(self.request_count, bool) or self.request_count < 3:
            raise ValueError("Bybit derivatives history request count is invalid")
        _validate_unique_ordered_points(self.open_interest, self.symbol, self.start_ms, self.end_ms)
        _validate_unique_ordered_points(self.account_ratio, self.symbol, self.start_ms, self.end_ms)
        _validate_unique_ordered_points(self.funding, self.symbol, self.start_ms, self.end_ms)
        if self.live_mainnet_order_routing_allowed or self.order_writes_supported:
            raise ValueError("Bybit derivatives history cannot grant order writes")


@dataclass(frozen=True)
class BybitDerivativesHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


Transport = Callable[[str, str, Mapping[str, str]], BybitDerivativesHttpJson]


class BybitHistoricalDerivativesClient:
    """Public GET-only derivatives-history client for point-in-time research evidence."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(
        self,
        *,
        host: str = "api.bybit.com",
        transport: Transport | None = None,
        maximum_pages_per_series: int = 250,
    ) -> None:
        self.host = validate_bybit_public_research_host(host)
        if not 1 <= maximum_pages_per_series <= 1000:
            raise ValueError("Bybit derivatives pagination bound must be within [1, 1000]")
        self._transport = _https_transport if transport is None else transport
        self._maximum_pages = maximum_pages_per_series

    def fetch_history(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str = "1h",
    ) -> BybitDerivativesHistory:
        _validate_symbol(symbol)
        _validate_range(start_ms, end_ms)
        if interval not in _ALLOWED_INTERVALS:
            raise ValueError("Bybit derivatives history interval is unsupported")
        open_interest, oi_requests = self.fetch_open_interest(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            interval=interval,
        )
        account_ratio, ratio_requests = self.fetch_account_ratio(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            interval=interval,
        )
        funding, funding_requests = self.fetch_funding(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        history = BybitDerivativesHistory(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            interval=interval,
            open_interest=open_interest,
            account_ratio=account_ratio,
            funding=funding,
            request_count=oi_requests + ratio_requests + funding_requests,
            host=self.host,
        )
        history.validate()
        return history

    def fetch_open_interest(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str,
    ) -> tuple[tuple[BybitOpenInterestPoint, ...], int]:
        _validate_series_request(symbol, start_ms, end_ms, interval)
        rows, request_count = self._fetch_cursor_series(
            path=_OPEN_INTEREST_PATH,
            query={
                "category": "linear",
                "symbol": symbol,
                "intervalTime": interval,
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": str(_MAX_OI_LIMIT),
            },
            limit=_MAX_OI_LIMIT,
        )
        result: list[BybitOpenInterestPoint] = []
        for row in rows:
            point = BybitOpenInterestPoint(
                symbol=symbol,
                timestamp_ms=_required_int(row, "timestamp"),
                open_interest=_required_decimal(row, "openInterest"),
                single_open_interest=_optional_decimal(row.get("singleOpenInterest")),
            )
            point.validate()
            result.append(point)
        return _dedupe_points(result, start_ms=start_ms, end_ms=end_ms), request_count

    def fetch_account_ratio(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str,
    ) -> tuple[tuple[BybitAccountRatioPoint, ...], int]:
        _validate_series_request(symbol, start_ms, end_ms, interval)
        rows, request_count = self._fetch_cursor_series(
            path=_ACCOUNT_RATIO_PATH,
            query={
                "category": "linear",
                "symbol": symbol,
                "period": interval,
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": str(_MAX_RATIO_LIMIT),
            },
            limit=_MAX_RATIO_LIMIT,
        )
        result: list[BybitAccountRatioPoint] = []
        for row in rows:
            row_symbol = row.get("symbol")
            if row_symbol != symbol:
                raise RuntimeError("Bybit account-ratio row symbol mismatch")
            point = BybitAccountRatioPoint(
                symbol=symbol,
                timestamp_ms=_required_int(row, "timestamp"),
                buy_ratio=_required_decimal(row, "buyRatio"),
                sell_ratio=_required_decimal(row, "sellRatio"),
            )
            point.validate()
            result.append(point)
        return _dedupe_points(result, start_ms=start_ms, end_ms=end_ms), request_count

    def fetch_funding(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[tuple[BybitHistoricalFundingPoint, ...], int]:
        _validate_symbol(symbol)
        _validate_range(start_ms, end_ms)
        start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
        end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
        window_start = start
        request_count = 0
        points: list[BybitHistoricalFundingPoint] = []
        while window_start < end:
            if request_count >= self._maximum_pages:
                raise RuntimeError("Bybit funding history exceeded bounded request count")
            window_end = min(end, window_start + timedelta(days=1))
            payload = self._get(
                _FUNDING_PATH,
                {
                    "category": "linear",
                    "symbol": symbol,
                    "startTime": str(int(window_start.timestamp() * 1000)),
                    "endTime": str(int(window_end.timestamp() * 1000)),
                    "limit": str(_MAX_FUNDING_LIMIT),
                },
            )
            request_count += 1
            result = _result_object(payload)
            raw_rows = result.get("list")
            if not isinstance(raw_rows, list):
                raise RuntimeError("Bybit funding history result.list must be an array")
            if len(raw_rows) >= _MAX_FUNDING_LIMIT:
                raise RuntimeError("Bybit funding daily window reached limit; possible truncation")
            for raw in raw_rows:
                if not isinstance(raw, Mapping):
                    raise RuntimeError("Bybit funding history row must be an object")
                if raw.get("symbol") != symbol:
                    raise RuntimeError("Bybit funding history row symbol mismatch")
                point = BybitHistoricalFundingPoint(
                    symbol=symbol,
                    timestamp_ms=_required_int(raw, "fundingRateTimestamp"),
                    funding_rate=_required_decimal(raw, "fundingRate"),
                )
                point.validate()
                points.append(point)
            window_start = window_end
        return _dedupe_points(points, start_ms=start_ms, end_ms=end_ms), request_count

    def _fetch_cursor_series(
        self,
        *,
        path: str,
        query: Mapping[str, str],
        limit: int,
    ) -> tuple[tuple[Mapping[str, Any], ...], int]:
        cursor = ""
        request_count = 0
        rows: list[Mapping[str, Any]] = []
        while True:
            request_count += 1
            if request_count > self._maximum_pages:
                raise RuntimeError("Bybit derivatives cursor pagination exceeded safety bound")
            active_query = dict(query)
            if cursor:
                active_query["cursor"] = cursor
            payload = self._get(path, active_query)
            result = _result_object(payload)
            raw_rows = result.get("list")
            if not isinstance(raw_rows, list):
                raise RuntimeError("Bybit derivatives history result.list must be an array")
            if len(raw_rows) > limit:
                raise RuntimeError("Bybit derivatives history response exceeded requested limit")
            for raw in raw_rows:
                if not isinstance(raw, Mapping):
                    raise RuntimeError("Bybit derivatives history row must be an object")
                rows.append(raw)
            next_cursor = result.get("nextPageCursor")
            if next_cursor in (None, ""):
                break
            if not isinstance(next_cursor, str) or next_cursor == cursor:
                raise RuntimeError("Bybit derivatives history cursor is invalid")
            cursor = next_cursor
        return tuple(rows), request_count

    def _get(self, path: str, query: Mapping[str, str]) -> Mapping[str, Any]:
        if path not in _ALLOWED_PATHS:
            raise ValueError("Bybit derivatives history rejected non-allowlisted path")
        query_string = urlencode(sorted(query.items()))
        response = self._transport(self.host, path, {"query": query_string})
        if response.status_code != 200:
            raise RuntimeError(
                f"Bybit derivatives history HTTP status {response.status_code}"
            )
        if response.payload.get("retCode") != 0:
            raise RuntimeError(
                f"Bybit derivatives history retCode {response.payload.get('retCode')}"
            )
        return response.payload


def _https_transport(
    host: str,
    path: str,
    metadata: Mapping[str, str],
) -> BybitDerivativesHttpJson:
    normalized_host = validate_bybit_public_research_host(host)
    if path not in _ALLOWED_PATHS:
        raise ValueError("Bybit derivatives transport rejected path")
    query_string = metadata.get("query", "")
    if not isinstance(query_string, str) or "#" in query_string:
        raise ValueError("Bybit derivatives transport rejected query")
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
        return BybitDerivativesHttpJson(
            status_code=response.status,
            headers=headers,
            payload=payload,
        )
    finally:
        connection.close()


def _result_object(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise RuntimeError("Bybit derivatives history result must be an object")
    return result


def _validate_series_request(symbol: str, start_ms: int, end_ms: int, interval: str) -> None:
    _validate_symbol(symbol)
    _validate_range(start_ms, end_ms)
    if interval not in _ALLOWED_INTERVALS:
        raise ValueError("Bybit derivatives history interval is unsupported")


def _validate_symbol(symbol: str) -> None:
    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise ValueError("Bybit derivatives symbol must be normalized USDT")
    base = symbol[:-4]
    if not base or not base.isalnum():
        raise ValueError("Bybit derivatives symbol contains invalid characters")


def _validate_range(start_ms: int, end_ms: int) -> None:
    _validate_timestamp(start_ms)
    _validate_timestamp(end_ms)
    if end_ms <= start_ms:
        raise ValueError("Bybit derivatives end_ms must be after start_ms")


def _validate_timestamp(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Bybit derivatives timestamp must be non-negative integer ms")


def _required_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise RuntimeError(f"Bybit derivatives history missing {field}")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Bybit derivatives history invalid {field}") from exc
    if parsed < 0:
        raise RuntimeError(f"Bybit derivatives history invalid {field}")
    return parsed


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value in (None, ""):
        raise RuntimeError(f"Bybit derivatives history missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(f"Bybit derivatives history invalid {field}") from exc
    if not parsed.is_finite():
        raise RuntimeError(f"Bybit derivatives history invalid {field}")
    return parsed


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("Bybit derivatives history invalid optional decimal") from exc
    if not parsed.is_finite():
        raise RuntimeError("Bybit derivatives history invalid optional decimal")
    return parsed


def _dedupe_points(
    points: list[Any],
    *,
    start_ms: int,
    end_ms: int,
) -> tuple[Any, ...]:
    by_time: dict[int, Any] = {}
    for point in points:
        timestamp = point.timestamp_ms
        if not start_ms <= timestamp <= end_ms:
            continue
        existing = by_time.get(timestamp)
        if existing is not None and existing != point:
            raise RuntimeError("Bybit derivatives history has conflicting duplicate timestamp")
        by_time[timestamp] = point
    return tuple(by_time[key] for key in sorted(by_time))


def _validate_unique_ordered_points(
    points: tuple[Any, ...],
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> None:
    previous: int | None = None
    for point in points:
        point.validate()
        if point.symbol != symbol:
            raise ValueError("Bybit derivatives history point symbol mismatch")
        if not start_ms <= point.timestamp_ms <= end_ms:
            raise ValueError("Bybit derivatives history point outside requested interval")
        if previous is not None and point.timestamp_ms <= previous:
            raise ValueError("Bybit derivatives history points must be strictly ordered")
        previous = point.timestamp_ms
