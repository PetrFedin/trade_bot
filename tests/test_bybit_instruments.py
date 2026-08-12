from dataclasses import replace
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from app.marketdata.bybit_instruments import (
    BybitInstrumentClient,
    BybitInstrumentHttpJson,
    BybitInstrumentSpec,
)


def _spec() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.10"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("500"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def test_market_quantity_rounds_down_and_never_increases_risk() -> None:
    spec = _spec()

    normalized = spec.normalize_market_quantity(
        Decimal("0.01299"),
        reference_price=Decimal("100000"),
    )

    assert normalized == Decimal("0.012")
    assert normalized < Decimal("0.01299")


def test_market_quantity_rejects_below_minimum_notional_after_rounding() -> None:
    spec = replace(
        _spec(),
        min_order_qty=Decimal("0.00001"),
        qty_step=Decimal("0.00001"),
    )

    normalized = spec.normalize_market_quantity(
        Decimal("0.00004"),
        reference_price=Decimal("100000"),
    )

    assert normalized is None


def test_target_and_stop_tick_rounding_are_conservative_for_both_sides() -> None:
    spec = _spec()

    assert spec.normalize_target_price("LONG", Decimal("101.01")) == Decimal("101.10")
    assert spec.normalize_protective_stop_price("LONG", Decimal("98.99")) == Decimal("98.90")
    assert spec.normalize_target_price("SHORT", Decimal("98.99")) == Decimal("98.90")
    assert spec.normalize_protective_stop_price("SHORT", Decimal("101.01")) == Decimal("101.10")


def test_instrument_client_parses_current_linear_contract_fields() -> None:
    calls: list[str] = []

    def transport(url: str, _headers: dict[str, str]) -> BybitInstrumentHttpJson:
        calls.append(url)
        query = parse_qs(urlsplit(url).query)
        symbol = query["symbol"][0]
        return BybitInstrumentHttpJson(
            status_code=200,
            headers={},
            payload={
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "category": "linear",
                    "list": [
                        {
                            "symbol": symbol,
                            "contractType": "LinearPerpetual",
                            "status": "Trading",
                            "baseCoin": symbol.removesuffix("USDT"),
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                            "priceFilter": {"tickSize": "0.10"},
                            "lotSizeFilter": {
                                "minNotionalValue": "5",
                                "maxMktOrderQty": "500",
                                "minOrderQty": "0.001",
                                "qtyStep": "0.001",
                            },
                            "leverageFilter": {"maxLeverage": "100.00"},
                            "fundingInterval": 480,
                        }
                    ],
                    "nextPageCursor": "",
                },
            },
        )

    specs = BybitInstrumentClient(transport=transport).fetch_symbols(
        ("BTCUSDT", "ETHUSDT")
    )

    assert set(specs) == {"BTCUSDT", "ETHUSDT"}
    assert specs["BTCUSDT"].qty_step == Decimal("0.001")
    assert specs["BTCUSDT"].funding_interval_minutes == 480
    assert specs["ETHUSDT"].status == "Trading"
    assert len(calls) == 2
    assert all("category=linear" in url for url in calls)
