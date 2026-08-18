from collections.abc import Mapping
from decimal import Decimal

import pytest

from app.execution.bybit_demo import BybitDemoHttpJson
from app.execution.bybit_demo_protection_client import (
    BybitDemoProtectionVerifiedOrderClient,
)


def _transport(rows: list[dict[str, object]]):
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


def _row(
    *,
    take_profit: object = "102000",
    stop_loss: object = "99500",
    trailing_stop: object = "500",
) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "0.012",
        "avgPrice": "100050",
        "unrealisedPnl": "1.25",
        "liqPrice": "97000",
        "takeProfit": take_profit,
        "stopLoss": stop_loss,
        "trailingStop": trailing_stop,
    }


def _client(rows: list[dict[str, object]]) -> BybitDemoProtectionVerifiedOrderClient:
    return BybitDemoProtectionVerifiedOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=_transport(rows),
        clock_ms=lambda: 1786982400000,
    )


def test_protection_client_exposes_exchange_reported_tp_sl_and_trailing() -> None:
    client = _client([_row()])

    position = client.get_positions()[0]

    assert client.protection_state_read_supported is True
    assert position.take_profit_price == Decimal("102000")
    assert position.stop_loss_price == Decimal("99500")
    assert position.trailing_stop_distance == Decimal("500")
    assert position.liquidation_price == Decimal("97000")


def test_protection_client_normalizes_zero_protection_fields_to_none() -> None:
    position = _client(
        [_row(take_profit="0", stop_loss="0.00", trailing_stop=0)]
    ).get_positions()[0]

    assert position.take_profit_price is None
    assert position.stop_loss_price is None
    assert position.trailing_stop_distance is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("takeProfit", "NaN"),
        ("stopLoss", "Infinity"),
        ("trailingStop", "-1"),
    ],
)
def test_protection_client_rejects_invalid_protection_values(
    field: str,
    value: str,
) -> None:
    row = _row()
    row[field] = value

    with pytest.raises(ValueError, match=field):
        _client([row]).get_positions()
