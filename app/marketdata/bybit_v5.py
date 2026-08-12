from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

_BYBIT_MAINNET_HOST = "api.bybit.com"
BYBIT_KLINE_URL = f"https://{_BYBIT_MAINNET_HOST}/v5/market/kline"
_ALLOWED_INTERVALS = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720"}


@dataclass(frozen=True)
class BybitKlineBar:
    symbol: str
    start_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("Bybit symbol must be normalized uppercase")
        if self.start_time.tzinfo is None or self.start_time.utcoffset() is None:
            raise ValueError("Bybit kline timestamp must be timezone-aware")
        for name, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"Bybit {name} must be positive and finite")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("Bybit kline high is inconsistent")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("Bybit kline low is inconsistent")
        if not self.volume.is_finite() or self.volume < 0:
            raise ValueError("Bybit volume must be non-negative and finite")
        if not self.turnover.is_finite() or self.turnover < 0:
            raise ValueError("Bybit turnover must be non-negative and finite")


@dataclass(frozen=True)
class BybitKlineRequest:
    symbols: tuple[str, ...]
    start_ms: int
    end_ms: int
    interval: str = "5"
    category: str = "linear"
    limit: int = 1000
    maximum_pages_per_symbol: int = 100

    def validate(self) -> None:
        if len(self.symbols) < 2:
            raise ValueError("Bybit crypto research requires at least two symbols")
        normalized = tuple(symbol.strip().upper() for symbol in self.symbols)
        if normalized != self.symbols or len(set(normalized)) != len(normalized):
            raise ValueError("Bybit symbols must be unique normalized uppercase")
        if self.category != "linear":
            raise ValueError("Bybit crypto research currently supports USDT/USDC linear only")
        if self.interval not in _ALLOWED_INTERVALS:
            raise ValueError("unsupported Bybit kline interval")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("invalid Bybit kline time range")
        if not 1 <= self.limit <= 1000:
            raise ValueError("Bybit kline limit must be within [1, 1000]")
        if self.maximum_pages_per_symbol < 1:
            raise ValueError("maximum_pages_per_symbol must be positive")


@dataclass(frozen=True)
class BybitHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class BybitKlineAcquisition:
    bars: tuple[BybitKlineBar, ...]
    pages_by_symbol: dict[str, int]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({bar.symbol for bar in self.bars}))

    def counts_by_symbol(self) -> dict[str, int]:
        return {
            symbol: sum(bar.symbol == symbol for bar in self.bars)
            for symbol in self.symbols
        }

    def validate(self, *, requested_symbols: tuple[str, ...], minimum_bars: int) -> None:
        observed = set(self.symbols)
        expected = set(requested_symbols)
        if observed != expected:
            raise ValueError(
                f"Bybit kline acquisition symbol mismatch:{sorted(expected - observed)}"
            )
        counts = self.counts_by_symbol()
        if any(count < minimum_bars for count in counts.values()):
            raise ValueError("Bybit kline acquisition has insufficient bars")
        if set(self.pages_by_symbol) != expected:
            raise ValueError("Bybit page accounting does not match requested symbols")
        previous: tuple[str, datetime] | None = None
        seen: set[tuple[str, datetime]] = set()
        for bar in self.bars:
            bar.validate()
            key = (bar.symbol, bar.start_time)
            if key in seen:
                raise ValueError("duplicate Bybit kline")
            if previous is not None and key < previous:
                raise ValueError("Bybit bars must be sorted by symbol then timestamp")
            seen.add(key)
            previous = key


Transport = Callable[[str, Mapping[str, str]], BybitHttpJson]


class BybitPublicKlineClient:
    """Read-only Bybit V5 historical kline client.

    Pagination walks backward by timestamp because `/v5/market/kline` returns newest
    rows first and does not expose a cursor. No API key is needed for public market data.
    """

    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = _https_transport if transport is None else transport

    def fetch(self, request: BybitKlineRequest) -> BybitKlineAcquisition:
        request.validate()
        bars: list[BybitKlineBar] = []
        pages_by_symbol: dict[str, int] = {}
        for symbol in request.symbols:
            symbol_bars, page_count = self._fetch_symbol(request, symbol)
            bars.extend(symbol_bars)
            pages_by_symbol[symbol] = page_count
        ordered = tuple(sorted(bars, key=lambda bar: (bar.symbol, bar.start_time)))
        acquisition = BybitKlineAcquisition(ordered, pages_by_symbol)
        acquisition.validate(requested_symbols=request.symbols, minimum_bars=1)
        return acquisition

    def _fetch_symbol(
        self,
        request: BybitKlineRequest,
        symbol: str,
    ) -> tuple[list[BybitKlineBar], int]:
        page_end = request.end_ms
        pages = 0
        result: dict[int, BybitKlineBar] = {}
        while page_end >= request.start_ms:
            if pages >= request.maximum_pages_per_symbol:
                raise ValueError(f"Bybit pagination exceeded maximum pages:{symbol}")
            url = _build_kline_url(request, symbol=symbol, end_ms=page_end)
            response = self._transport(url, {"Accept": "application/json"})
            pages += 1
            if response.status_code != 200:
                raise ValueError(f"Bybit kline request failed:{response.status_code}")
            if response.payload.get("retCode") != 0:
                raise ValueError(f"Bybit kline API error:{response.payload.get('retMsg')}")
            rows = _response_rows(response.payload, expected_symbol=symbol)
            if not rows:
                break
            starts: list[int] = []
            for row in rows:
                start_ms, bar = _parse_kline(symbol, row)
                starts.append(start_ms)
                if request.start_ms <= start_ms <= request.end_ms:
                    result[start_ms] = bar
            oldest = min(starts)
            if oldest <= request.start_ms:
                break
            next_end = oldest - 1
            if next_end >= page_end:
                raise RuntimeError("Bybit kline pagination did not move backward")
            page_end = next_end
        return [result[key] for key in sorted(result)], pages


def _build_kline_url(request: BybitKlineRequest, *, symbol: str, end_ms: int) -> str:
    params = {
        "category": request.category,
        "symbol": symbol,
        "interval": request.interval,
        "start": str(request.start_ms),
        "end": str(end_ms),
        "limit": str(request.limit),
    }
    return f"{BYBIT_KLINE_URL}?{urlencode(params)}"


def _response_rows(payload: Mapping[str, Any], *, expected_symbol: str) -> list[list[Any]]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Bybit kline response missing result")
    if result.get("symbol") != expected_symbol:
        raise ValueError("Bybit kline response symbol mismatch")
    rows = result.get("list")
    if not isinstance(rows, list):
        raise ValueError("Bybit kline response missing list")
    if not all(isinstance(row, list) for row in rows):
        raise ValueError("Bybit kline rows must be lists")
    return rows


def _parse_kline(symbol: str, row: list[Any]) -> tuple[int, BybitKlineBar]:
    if len(row) < 7:
        raise ValueError("Bybit kline row is too short")
    try:
        start_ms = int(row[0])
        bar = BybitKlineBar(
            symbol=symbol,
            start_time=datetime.fromtimestamp(start_ms / 1000, tz=UTC),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
            turnover=Decimal(str(row[6])),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Bybit kline row") from exc
    bar.validate()
    return start_ms, bar


def interval_milliseconds(interval: str) -> int:
    if interval not in _ALLOWED_INTERVALS:
        raise ValueError("unsupported Bybit interval")
    return int(interval) * 60_000


def last_completed_kline_end_ms(*, now_ms: int, interval: str) -> int:
    interval_ms = interval_milliseconds(interval)
    if now_ms < interval_ms:
        raise ValueError("now_ms is too early for completed kline resolution")
    current_bucket_start = (now_ms // interval_ms) * interval_ms
    return current_bucket_start - 1


def _https_transport(url: str, headers: Mapping[str, str]) -> BybitHttpJson:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != _BYBIT_MAINNET_HOST:
        raise ValueError("Bybit market-data transport rejected non-allowlisted endpoint")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Bybit market-data transport rejected ambiguous URL authority")
    if parsed.port not in (None, 443):
        raise ValueError("Bybit market-data transport requires HTTPS port 443")
    if parsed.path != "/v5/market/kline":
        raise ValueError("Bybit market-data transport rejected unexpected path")
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = HTTPSConnection(_BYBIT_MAINNET_HOST, 443, timeout=30)
    try:
        connection.request("GET", target, headers=dict(headers))
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Bybit response must be a JSON object")
        return BybitHttpJson(
            status_code=response.status,
            headers={key: value for key, value in response.getheaders()},
            payload=payload,
        )
    finally:
        connection.close()
