from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

_BYBIT_DEMO_HOST = "api-demo.bybit.com"
_BYBIT_DEMO_TICKERS_PATH = "/v5/market/tickers"
BYBIT_DEMO_TICKERS_URL = f"https://{_BYBIT_DEMO_HOST}{_BYBIT_DEMO_TICKERS_PATH}"
_DEFAULT_MAXIMUM_QUOTE_AGE_MS = 5_000
_DEFAULT_MAXIMUM_FUTURE_SKEW_MS = 1_000


class BybitDemoQuoteError(ValueError):
    """Base class for safe, classifiable demo quote failures."""


class BybitDemoQuoteHttpError(BybitDemoQuoteError):
    pass


class BybitDemoQuoteApiError(BybitDemoQuoteError):
    pass


class BybitDemoQuoteTimestampError(BybitDemoQuoteError):
    pass


class BybitDemoQuoteStaleError(BybitDemoQuoteTimestampError):
    pass


class BybitDemoQuoteFutureTimestampError(BybitDemoQuoteTimestampError):
    pass


class BybitDemoQuoteShapeError(BybitDemoQuoteError):
    pass


class BybitDemoQuoteSymbolError(BybitDemoQuoteError):
    pass


class BybitDemoQuotePriceError(BybitDemoQuoteError):
    pass


class BybitDemoQuoteCrossedBookError(BybitDemoQuotePriceError):
    pass


@dataclass(frozen=True)
class BybitDemoMarketQuote:
    symbol: str
    last_price: Decimal
    mark_price: Decimal
    bid_price: Decimal
    ask_price: Decimal
    server_time_ms: int
    received_time_ms: int
    age_ms: int

    def validate(self) -> None:
        if self.symbol != self.symbol.strip().upper() or not self.symbol.endswith("USDT"):
            raise BybitDemoQuoteSymbolError(
                "Bybit demo quote symbol must be normalized USDT"
            )
        for name, value in (
            ("last_price", self.last_price),
            ("mark_price", self.mark_price),
            ("bid_price", self.bid_price),
            ("ask_price", self.ask_price),
        ):
            if not value.is_finite() or value <= 0:
                raise BybitDemoQuotePriceError(
                    f"Bybit demo quote {name} must be positive and finite"
                )
        if self.bid_price > self.ask_price:
            raise BybitDemoQuoteCrossedBookError(
                "Bybit demo quote bid cannot exceed ask"
            )
        if self.server_time_ms < 0 or self.received_time_ms < 0:
            raise BybitDemoQuoteTimestampError(
                "Bybit demo quote timestamps cannot be negative"
            )
        if self.age_ms != self.received_time_ms - self.server_time_ms:
            raise BybitDemoQuoteTimestampError(
                "Bybit demo quote age does not reconcile with timestamps"
            )


@dataclass(frozen=True)
class BybitDemoQuoteHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


Transport = Callable[[str, Mapping[str, str]], BybitDemoQuoteHttpJson]
ClockMs = Callable[[], int]


class BybitDemoMarketQuoteClient:
    """Public read-only demo quote client with server-time freshness validation."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        clock_ms: ClockMs | None = None,
        maximum_quote_age_ms: int = _DEFAULT_MAXIMUM_QUOTE_AGE_MS,
        maximum_future_skew_ms: int = _DEFAULT_MAXIMUM_FUTURE_SKEW_MS,
    ) -> None:
        if maximum_quote_age_ms < 0:
            raise ValueError("Bybit demo maximum quote age cannot be negative")
        if maximum_future_skew_ms < 0:
            raise ValueError("Bybit demo maximum future quote skew cannot be negative")
        self._transport = _https_transport if transport is None else transport
        self._clock_ms = (lambda: int(time.time() * 1000)) if clock_ms is None else clock_ms
        self._maximum_quote_age_ms = maximum_quote_age_ms
        self._maximum_future_skew_ms = maximum_future_skew_ms

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    def get_quote(self, *, symbol: str) -> BybitDemoMarketQuote:
        _validate_symbol(symbol)
        url = f"{BYBIT_DEMO_TICKERS_URL}?{urlencode({'category': 'linear', 'symbol': symbol})}"
        response = self._transport(url, {"Accept": "application/json"})
        received_time_ms = self._clock_ms()
        if response.status_code != 200:
            raise BybitDemoQuoteHttpError(
                f"Bybit demo quote HTTP request failed:{response.status_code}"
            )
        if response.payload.get("retCode") != 0:
            raise BybitDemoQuoteApiError("Bybit demo quote API returned non-zero retCode")
        server_time_ms = _non_negative_int(response.payload, "time")
        age_ms = received_time_ms - server_time_ms
        if age_ms > self._maximum_quote_age_ms:
            raise BybitDemoQuoteStaleError("Bybit demo quote response is stale")
        if age_ms < -self._maximum_future_skew_ms:
            raise BybitDemoQuoteFutureTimestampError(
                "Bybit demo quote response timestamp is too far in the future"
            )

        result = response.payload.get("result")
        if not isinstance(result, Mapping) or result.get("category") != "linear":
            raise BybitDemoQuoteShapeError(
                "Bybit demo quote response missing linear result"
            )
        rows = result.get("list")
        if not isinstance(rows, list):
            raise BybitDemoQuoteShapeError("Bybit demo quote response missing list")
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping) and row.get("symbol") == symbol
        ]
        if len(matches) != 1:
            raise BybitDemoQuoteSymbolError(
                f"Bybit demo quote response missing exact {symbol}"
            )
        row = matches[0]
        quote = BybitDemoMarketQuote(
            symbol=symbol,
            last_price=_positive_decimal(row, "lastPrice"),
            mark_price=_positive_decimal(row, "markPrice"),
            bid_price=_positive_decimal(row, "bid1Price"),
            ask_price=_positive_decimal(row, "ask1Price"),
            server_time_ms=server_time_ms,
            received_time_ms=received_time_ms,
            age_ms=age_ms,
        )
        quote.validate()
        return quote


def _validate_symbol(symbol: str) -> None:
    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise BybitDemoQuoteSymbolError(
            "Bybit demo quote symbol must be normalized USDT linear symbol"
        )


def _positive_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value in (None, ""):
        raise BybitDemoQuotePriceError(
            f"Bybit demo quote response missing {field}"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BybitDemoQuotePriceError(
            f"Bybit demo quote response has invalid {field}"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise BybitDemoQuotePriceError(
            f"Bybit demo quote response has non-positive {field}"
        )
    return parsed


def _non_negative_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or value is None or value == "":
        raise BybitDemoQuoteTimestampError(
            f"Bybit demo quote response missing {field}"
        )
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise BybitDemoQuoteTimestampError(
            f"Bybit demo quote response has invalid {field}"
        ) from exc
    if parsed < 0:
        raise BybitDemoQuoteTimestampError(
            f"Bybit demo quote response has invalid {field}"
        )
    return parsed


def _https_transport(url: str, headers: Mapping[str, str]) -> BybitDemoQuoteHttpJson:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != _BYBIT_DEMO_HOST:
        raise BybitDemoQuoteShapeError(
            "Bybit demo quote transport rejected non-demo endpoint"
        )
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise BybitDemoQuoteShapeError(
            "Bybit demo quote transport rejected ambiguous URL authority"
        )
    if parsed.port not in (None, 443):
        raise BybitDemoQuoteShapeError(
            "Bybit demo quote transport requires HTTPS port 443"
        )
    if parsed.path != _BYBIT_DEMO_TICKERS_PATH:
        raise BybitDemoQuoteShapeError(
            "Bybit demo quote transport rejected non-ticker path"
        )
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = HTTPSConnection(_BYBIT_DEMO_HOST, 443, timeout=10)
    try:
        connection.request("GET", target, headers=dict(headers))
        response = connection.getresponse()
        try:
            payload = json.loads(response.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BybitDemoQuoteShapeError(
                "Bybit demo quote response returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise BybitDemoQuoteShapeError(
                "Bybit demo quote response must be a JSON object"
            )
        return BybitDemoQuoteHttpJson(
            status_code=response.status,
            headers={key: value for key, value in response.getheaders()},
            payload=payload,
        )
    finally:
        connection.close()
