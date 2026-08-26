from __future__ import annotations

from app.marketdata.bybit_demo_instruments import (
    BYBIT_DEMO_INSTRUMENT_URL,
    BybitDemoInstrumentClient,
    BybitDemoInstrumentHttpJson,
)


def _payload(symbol: str):
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "linear",
            "list": [
                {
                    "symbol": symbol,
                    "contractType": "LinearPerpetual",
                    "status": "Trading",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "settleCoin": "USDT",
                    "fundingInterval": "480",
                    "priceFilter": {"tickSize": "0.1"},
                    "lotSizeFilter": {
                        "minOrderQty": "0.001",
                        "qtyStep": "0.001",
                        "minNotionalValue": "5",
                        "maxMktOrderQty": "100",
                    },
                    "leverageFilter": {"maxLeverage": "100"},
                }
            ],
        },
    }


def test_demo_instrument_reader_uses_demo_domain_and_current_exchange_limits() -> None:
    seen: list[str] = []

    def _transport(url, _headers):
        seen.append(url)
        return BybitDemoInstrumentHttpJson(
            status_code=200,
            headers={},
            payload=_payload("BTCUSDT"),
        )

    spec = BybitDemoInstrumentClient(transport=_transport).fetch_symbol("BTCUSDT")

    assert seen == [f"{BYBIT_DEMO_INSTRUMENT_URL}?category=linear&symbol=BTCUSDT"]
    assert spec.symbol == "BTCUSDT"
    assert str(spec.max_market_order_qty) == "100"
    assert spec.quote_coin == "USDT"
    assert spec.settle_coin == "USDT"


def test_demo_instrument_reader_rejects_non_usdt_symbol_before_transport() -> None:
    called = False

    def _transport(_url, _headers):
        nonlocal called
        called = True
        raise AssertionError("transport must not run")

    try:
        BybitDemoInstrumentClient(transport=_transport).fetch_symbol("BTCUSD")
    except ValueError as exc:
        assert "USDT" in str(exc)
    else:
        raise AssertionError("non-USDT symbol must be rejected")
    assert called is False
