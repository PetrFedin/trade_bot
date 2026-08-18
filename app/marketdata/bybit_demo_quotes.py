from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

_BYBIT_DEMO_HOST = "api-demo.bybit.com"
_BYBIT_DEMO_TICKERS_PATH = "/v5/market/tickers"
BYBIT_DEMO_TICKERS_URL = f"https://{_BYBIT_DEMO_HOST}{_BYBIT_DEMO_TICKERS_PATH}"


@dataclass(frozen=True)
class BybitDemoMarketQuote:
    symbol: str
    last_price: Decimal
    mark_price: Decimal
    bid_price: Decimal
    ask_price: Decimal

    def validate(self) -> None:
        if self.symbol != self.symbol.strip().upper() or not self.symbol.endswith("USDT"):
            raise ValueError("Bybit demo quote symbol must be normalized USDT")
        for name, value in (
            ("last_price", self.last_price),
            ("mark_price", self.mark_price),
            ("bid_price", self.bid_price),
            ("ask_price", self.ask_price),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"Bybit demo quote {name} must be positive and finite")
        if self.bid_price > self.ask_price:
            raise ValueError("Bybit demo quote bid cannot exceed ask")


@dataclass(frozen=True)
class BybitDemoQuoteHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


Transport = Callable[[str, Mapping[str, str]], BybitDemoQuoteHttpJson]


class BybitDemoMarketQuoteClient:
    """Public read-only demo-market quote client used before an explicit demo write."""

    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = _https_transport if transport is None else transport

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    def get_quote(self, *, symbol: str) -> BybitDemoMarketQuote:
        _validate_symbol(symbol)
        url = f"{BYBIT_DEMO_TICKERS_URL}?{urlencode({'category': 'linear', 'symbol': symbol})}"
        response = self._transport(url, {"Accept": "application/json"})
        if response.status_code != 200:
            raise ValueError(f"Bybit demo quote HTTP request failed:{response.status_code}")
        if response.payload.get("retCode") != 0:
            raise ValueError(f"Bybit demo quote API error:{response.payload.get('retMsg')}")
        result = response.payload.get("result")
        if not isinstance(result, Mapping) or result.get("category") != "linear":
            raise ValueError("Bybit demo quote response missing linear result")
        rows = result.get("list")
        if not isinstance(rows, list):
            raise ValueError("Bybit demo quote response missing list")
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping) and row.get("symbol") == symbol
        ]
        if len(matches) != 1:
            raise ValueError(f"Bybit demo quote response missing exact {symbol}")
        row = matches[0]
        quote = BybitDemoMarketQuote(
            symbol=symbol,
            last_price=_positive_decimal(row, "lastPrice"),
            mark_price=_positive_decimal(row, "markPrice"),
            bid_price=_positive_decimal(row, "bid1Price"),
            ask_price=_positive_decimal(row, "ask1Price"),
        )
        quote.validate()
        return quote


def _validate_symbol(symbol: str) -> None:
    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise ValueError("Bybit demo quote symbol must be normalized USDT linear symbol")


def _positive_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value in (None, ""):
        raise ValueError(f"Bybit demo quote response missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Bybit demo quote response has invalid {field}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"Bybit demo quote response has non-positive {field}")
    return parsed


def _https_transport(url: str, headers: Mapping[str, str]) -> BybitDemoQuoteHttpJson:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != _BYBIT_DEMO_HOST:
        raise ValueError("Bybit demo quote transport rejected non-demo endpoint")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Bybit demo quote transport rejected ambiguous URL authority")
    if parsed.port not in (None, 443):
        raise ValueError("Bybit demo quote transport requires HTTPS port 443")
    if parsed.path != _BYBIT_DEMO_TICKERS_PATH:
        raise ValueError("Bybit demo quote transport rejected non-ticker path")
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = HTTPSConnection(_BYBIT_DEMO_HOST, 443, timeout=10)
    try:
        connection.request("GET", target, headers=dict(headers))
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Bybit demo quote response must be a JSON object")
        return BybitDemoQuoteHttpJson(
            status_code=response.status,
            headers={key: value for key, value in response.getheaders()},
            payload=payload,
        )
    finally:
        connection.close()
