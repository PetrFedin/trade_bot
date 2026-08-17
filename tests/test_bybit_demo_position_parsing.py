from collections.abc import Mapping
from decimal import Decimal

import pytest

from app.execution.bybit_demo import BybitDemoHttpJson, BybitDemoOrderClient


def _transport_with_positions(
    rows: list[dict[str, object]],
):
    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: str | None,
    ) -> BybitDemoHttpJson:
        assert method == "GET"
        assert url.startswith("https://api-demo.bybit.com/v5/position/list?")
        assert headers["X-BAPI-API-KEY"] == "demo-key"
        assert body is None
        return BybitDemoHttpJson(
            status_code=200,
            headers={},
            payload={"retCode": 0, "result": {"list": rows}},
        )

    return transport


def _row(*, liq_price: object) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "0.012",
        "avgPrice": "100050",
        "unrealisedPnl": "1.25",
        "liqPrice": liq_price,
    }


def _client(rows: list[dict[str, object]]) -> BybitDemoOrderClient:
    return BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=_transport_with_positions(rows),
        clock_ms=lambda: 1786982400000,
    )


def test_position_parser_exposes_valid_liquidation_price() -> None:
    positions = _client([_row(liq_price="97000")]).get_positions()

    assert len(positions) == 1
    position = positions[0]
    assert position.symbol == "BTCUSDT"
    assert position.liquidation_price == Decimal("97000")


def test_position_parser_preserves_empty_liquidation_price_as_unavailable() -> None:
    position = _client([_row(liq_price="")]).get_positions()[0]

    assert position.liquidation_price is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "not-a-number"])
def test_position_parser_rejects_invalid_liquidation_price(value: str) -> None:
    with pytest.raises(ValueError, match="liqPrice"):
        _client([_row(liq_price=value)]).get_positions()
