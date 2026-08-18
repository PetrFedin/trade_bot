from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from app.marketdata.bybit_demo_quotes import (
    BybitDemoMarketQuoteClient,
    BybitDemoQuoteHttpJson,
)


def _payload(
    *,
    bid: str = "99.9",
    ask: str = "100.1",
    server_time_ms: int = 1_000_000,
) -> dict[str, object]:
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
        "time": server_time_ms,
    }


def _client(transport, *, clock_ms: int = 1_000_250, **kwargs):
    return BybitDemoMarketQuoteClient(
        transport=transport,
        clock_ms=lambda: clock_ms,
        **kwargs,
    )


def test_demo_quote_client_uses_public_demo_ticker_endpoint() -> None:
    observed: dict[str, object] = {}

    def transport(url: str, headers: dict[str, str]) -> BybitDemoQuoteHttpJson:
        observed["url"] = url
        observed["headers"] = headers
        return BybitDemoQuoteHttpJson(200, {}, _payload())

    client = _client(transport)
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
    assert quote.server_time_ms == 1_000_000
    assert quote.received_time_ms == 1_000_250
    assert quote.age_ms == 250
    assert client.live_mainnet_order_routing_allowed is False


def test_demo_quote_client_fails_closed_on_crossed_book() -> None:
    def transport(_: str, __: dict[str, str]) -> BybitDemoQuoteHttpJson:
        return BybitDemoQuoteHttpJson(200, {}, _payload(bid="101", ask="100"))

    with pytest.raises(ValueError, match="bid cannot exceed ask"):
        _client(transport).get_quote(symbol="BTCUSDT")


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
        _client(missing_symbol).get_quote(symbol="BTCUSDT")

    def zero_bid(_: str, __: dict[str, str]) -> BybitDemoQuoteHttpJson:
        return BybitDemoQuoteHttpJson(200, {}, _payload(bid="0"))

    with pytest.raises(ValueError, match="non-positive bid1Price"):
        _client(zero_bid).get_quote(symbol="BTCUSDT")


def test_demo_quote_client_rejects_stale_server_timestamp() -> None:
    def stale(_: str, __: dict[str, str]) -> BybitDemoQuoteHttpJson:
        return BybitDemoQuoteHttpJson(
            200,
            {},
            _payload(server_time_ms=990_000),
        )

    with pytest.raises(ValueError, match="stale"):
        _client(stale, clock_ms=1_000_001, maximum_quote_age_ms=5_000).get_quote(
            symbol="BTCUSDT"
        )


def test_demo_quote_client_rejects_timestamp_too_far_in_future() -> None:
    def future(_: str, __: dict[str, str]) -> BybitDemoQuoteHttpJson:
        return BybitDemoQuoteHttpJson(
            200,
            {},
            _payload(server_time_ms=1_002_000),
        )

    with pytest.raises(ValueError, match="too far in the future"):
        _client(
            future,
            clock_ms=1_000_000,
            maximum_future_skew_ms=1_000,
        ).get_quote(symbol="BTCUSDT")


def test_demo_quote_client_rejects_missing_response_time() -> None:
    def missing_time(_: str, __: dict[str, str]) -> BybitDemoQuoteHttpJson:
        payload = _payload()
        payload.pop("time")
        return BybitDemoQuoteHttpJson(200, {}, payload)

    with pytest.raises(ValueError, match="missing time"):
        _client(missing_time).get_quote(symbol="BTCUSDT")
