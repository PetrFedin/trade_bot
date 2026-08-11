from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.marketdata.ohlcv import MultiSymbolOhlcvDataset, OhlcvBar, normalize_bars

ALPACA_STOCK_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"


@dataclass(frozen=True)
class AlpacaHistoricalBarsRequest:
    symbols: tuple[str, ...]
    start: datetime
    end: datetime
    timeframe: str = "1Day"
    feed: str = "iex"
    adjustment: str = "all"
    currency: str = "USD"
    limit: int = 10_000
    maximum_pages: int = 100

    def validate(self) -> None:
        if not self.symbols:
            raise ValueError("historical request requires symbols")
        normalized = tuple(symbol.strip().upper() for symbol in self.symbols)
        if normalized != self.symbols or len(set(normalized)) != len(normalized):
            raise ValueError("historical request symbols must be unique normalized uppercase")
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("historical request start must be timezone-aware")
        if self.end.tzinfo is None or self.end.utcoffset() is None:
            raise ValueError("historical request end must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("historical request end must be after start")
        if self.feed not in {"iex", "sip", "boats", "otc"}:
            raise ValueError("unsupported Alpaca stock feed")
        if self.adjustment not in {"raw", "split", "dividend", "all"}:
            raise ValueError("unsupported Alpaca adjustment")
        if self.currency != "USD":
            raise ValueError("research boundary currently requires USD")
        if not 1 <= self.limit <= 10_000:
            raise ValueError("historical request limit must be within [1, 10000]")
        if self.maximum_pages < 1:
            raise ValueError("maximum_pages must be positive")


@dataclass(frozen=True)
class HttpJsonPage:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class HistoricalAcquisition:
    dataset: MultiSymbolOhlcvDataset
    page_count: int
    missing_symbols: tuple[str, ...]

    def validate(self, *, minimum_bars_per_symbol: int = 1) -> None:
        self.dataset.validate(minimum_symbols=1)
        if self.page_count < 1:
            raise ValueError("historical acquisition page_count must be positive")
        if self.missing_symbols:
            raise ValueError("historical acquisition has missing symbols")
        counts = self.dataset.counts_by_symbol()
        if any(count < minimum_bars_per_symbol for count in counts.values()):
            raise ValueError("historical acquisition has insufficient bars per symbol")


Transport = Callable[[str, Mapping[str, str]], HttpJsonPage]


class AlpacaHistoricalBarsClient:
    def __init__(
        self,
        *,
        key_id: str,
        secret_key: str,
        transport: Transport | None = None,
    ) -> None:
        if not key_id.strip() or not secret_key.strip():
            raise ValueError("Alpaca market-data credentials are required")
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
        }
        self._transport = _urllib_transport if transport is None else transport

    def fetch(self, request: AlpacaHistoricalBarsRequest) -> HistoricalAcquisition:
        request.validate()
        bars: list[OhlcvBar] = []
        request_ids: list[str] = []
        seen_tokens: set[str] = set()
        page_token: str | None = None
        page_count = 0

        while True:
            if page_count >= request.maximum_pages:
                raise ValueError("Alpaca historical pagination exceeded maximum_pages")
            url = _build_url(request, page_token=page_token)
            page = self._transport(url, self._headers)
            page_count += 1
            if page.status_code != 200:
                raise ValueError(f"Alpaca historical request failed:{page.status_code}")
            request_id = _header(page.headers, "x-request-id")
            if request_id:
                request_ids.append(request_id)
            bars.extend(_parse_multi_symbol_bars(page.payload, request.symbols))
            raw_token = page.payload.get("next_page_token")
            if raw_token in (None, ""):
                break
            if not isinstance(raw_token, str):
                raise ValueError("Alpaca next_page_token must be a string or null")
            if raw_token in seen_tokens:
                raise ValueError("Alpaca historical pagination token repeated")
            seen_tokens.add(raw_token)
            page_token = raw_token

        dataset = MultiSymbolOhlcvDataset(
            provider="alpaca",
            feed=request.feed,
            timeframe=request.timeframe,
            adjustment=request.adjustment,
            bars=normalize_bars(bars),
            request_ids=tuple(request_ids),
        )
        requested = set(request.symbols)
        observed = set(dataset.symbols)
        unexpected = observed - requested
        if unexpected:
            raise ValueError(f"Alpaca returned unexpected symbols:{sorted(unexpected)}")
        return HistoricalAcquisition(
            dataset=dataset,
            page_count=page_count,
            missing_symbols=tuple(sorted(requested - observed)),
        )


def _build_url(
    request: AlpacaHistoricalBarsRequest, *, page_token: str | None
) -> str:
    params = {
        "symbols": ",".join(request.symbols),
        "timeframe": request.timeframe,
        "start": _rfc3339(request.start),
        "end": _rfc3339(request.end),
        "limit": str(request.limit),
        "adjustment": request.adjustment,
        "feed": request.feed,
        "currency": request.currency,
        "sort": "asc",
    }
    if page_token is not None:
        params["page_token"] = page_token
    return f"{ALPACA_STOCK_BARS_URL}?{urlencode(params)}"


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _parse_multi_symbol_bars(
    payload: Mapping[str, Any], requested_symbols: Sequence[str]
) -> list[OhlcvBar]:
    raw_bars = payload.get("bars")
    if not isinstance(raw_bars, Mapping):
        raise ValueError("Alpaca historical response missing bars object")
    parsed: list[OhlcvBar] = []
    requested = set(requested_symbols)
    for symbol, rows in raw_bars.items():
        if not isinstance(symbol, str) or symbol not in requested:
            raise ValueError("Alpaca historical response contains unexpected symbol")
        if not isinstance(rows, list):
            raise ValueError("Alpaca historical symbol bars must be a list")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("Alpaca historical bar must be an object")
            parsed.append(_parse_bar(symbol, row))
    return parsed


def _parse_bar(symbol: str, row: Mapping[str, Any]) -> OhlcvBar:
    timestamp = row.get("t")
    if not isinstance(timestamp, str):
        raise ValueError("Alpaca bar timestamp missing")
    normalized_timestamp = timestamp.replace("Z", "+00:00")
    bar = OhlcvBar(
        symbol=symbol,
        timestamp=datetime.fromisoformat(normalized_timestamp),
        open=_price(row, "o"),
        high=_price(row, "h"),
        low=_price(row, "l"),
        close=_price(row, "c"),
        volume=_non_negative_int(row, "v"),
        trade_count=_non_negative_int(row, "n"),
        vwap=_optional_price(row, "vw"),
    )
    bar.validate()
    return bar


def _price(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"Alpaca bar {field} missing")
    return Decimal(str(value))


def _optional_price(row: Mapping[str, Any], field: str) -> Decimal | None:
    value = row.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"Alpaca bar {field} invalid")
    return Decimal(str(value))


def _non_negative_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Alpaca bar {field} must be a non-negative integer")
    return value


def _urllib_transport(url: str, headers: Mapping[str, str]) -> HttpJsonPage:
    request = Request(url, headers=dict(headers), method="GET")
    with urlopen(request, timeout=30) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Alpaca historical response must be a JSON object")
        return HttpJsonPage(
            status_code=response.status,
            headers=dict(response.headers.items()),
            payload=payload,
        )
