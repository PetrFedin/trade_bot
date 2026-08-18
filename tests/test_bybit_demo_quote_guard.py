from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from app.marketdata.bybit_demo_quotes import (
    BybitDemoMarketQuoteClient,
    BybitDemoQuoteHttpJson,
)


def _payload(*, bid: str = "99.9", ask: str = "100.1") -> dict[str, object]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "linear",
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "lastPrice": "100",
                    "markPrice": "100.02",
                    "bid1Price": bid,
                    "ask1Price": ask,
                }
            ],
        },
    }


def test_demo_quote_client_uses_public_demo_ticker_endpoint() -> None:
    observed: dict[str, object] = {}

    def transport(url: str, headers: dict[str, str]) -> BybitDemoQuoteHttpJson:
        observed["url"] = url
        observed["headers"] = headers
        return BybitDemoQuoteHttpJson(200, {}, _payload())

    client = BybitDemoMarketQuoteClient(transport=transport)
    quote = client.get_quote(symbol="BTCUSDT")

    parsed = urlsplit(str(observed["url"]))
    assert parsed.scheme == "https"
    assert parsed.hostname == "api-demo.bybit.com"
    assert parsed.path == "/v5/market/tickers"
    assert parse_qs(parsed.query) == {
        "category": ["linear"],
        "symbol": ["BTCUSDT"],
    }
    assert observed["headers"] == {"Accept": "application/json"}
    assert quote.bid_price == Decimal("99.9")
    assert quote.ask_price == Decimal("100.1")
    assert quote.mark_price == Decimal("100.02")
    assert client.live_mainnet_order_routing_allowed is False


def test_demo_quote_client_fails_closed_on_crossed_book() -> None:
    def transport(_: str, __: dict[str, str]) -> BybitDemoQuoteHttpJson:
        return BybitDemoQuoteHttpJson(200, {}, _payload(bid="101", ask="100"))

    with pytest.raises(ValueError, match="bid cannot exceed ask"):
        BybitDemoMarketQuoteClient(transport=transport).get_quote(symbol="BTCUSDT")


def test_demo_quote_client_requires_exact_symbol_and_positive_prices() -> None:
    def missing_symbol(_: str, __: dict[str, str]) -> BybitDemoQuoteHttpJson:
        payload = _payload()
        result = payload["result"]
        assert isinstance(result, dict)
        rows = result["list"]
        assert isinstance(rows, list)
        row = rows[0]
        assert isinstance(row, dict)
        row["symbol"] = "ETHUSDT"
        return BybitDemoQuoteHttpJson(200, {}, payload)

    with pytest.raises(ValueError, match="missing exact BTCUSDT"):
        BybitDemoMarketQuoteClient(transport=missing_symbol).get_quote(symbol="BTCUSDT")

    def zero_bid(_: str, __: dict[str, str]) -> BybitDemoQuoteHttpJson:
        return BybitDemoQuoteHttpJson(200, {}, _payload(bid="0"))

    with pytest.raises(ValueError, match="non-positive bid1Price"):
        BybitDemoMarketQuoteClient(transport=zero_bid).get_quote(symbol="BTCUSDT")
