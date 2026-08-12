import json
from decimal import Decimal

import pytest

from app.execution.bybit_demo import (
    BybitDemoHttpJson,
    BybitDemoOrderClient,
    BybitDemoProtectionRequest,
)


def test_demo_gateway_sets_full_position_tp_sl_on_demo_endpoint() -> None:
    captured: dict[str, object] = {}

    def transport(
        method: str,
        url: str,
        headers: dict[str, str],
        body: str | None,
    ) -> BybitDemoHttpJson:
        captured.update(method=method, url=url, headers=headers, body=body)
        return BybitDemoHttpJson(
            status_code=200,
            headers={},
            payload={"retCode": 0, "retMsg": "OK", "result": {}},
        )

    client = BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=transport,
        clock_ms=lambda: 1_700_000_000_000,
    )
    acknowledgement = client.set_full_position_protection(
        BybitDemoProtectionRequest(
            symbol="BTCUSDT",
            side="Buy",
            average_entry_price=Decimal("100000"),
            take_profit_price=Decimal("101500"),
            stop_loss_price=Decimal("99500"),
        )
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api-demo.bybit.com/v5/position/trading-stop"
    payload = json.loads(str(captured["body"]))
    assert payload == {
        "category": "linear",
        "positionIdx": 0,
        "slTriggerBy": "LastPrice",
        "stopLoss": "99500",
        "symbol": "BTCUSDT",
        "takeProfit": "101500",
        "tpTriggerBy": "LastPrice",
        "tpslMode": "Full",
    }
    assert acknowledgement.accepted is True
    assert acknowledgement.environment == "BYBIT_DEMO"
    assert acknowledgement.live_mainnet_order is False
    assert client.live_mainnet_order_routing_allowed is False


def test_demo_protection_rejects_levels_that_do_not_bracket_actual_entry() -> None:
    client = BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=lambda *_args: BybitDemoHttpJson(200, {}, {"retCode": 0}),
    )

    with pytest.raises(ValueError, match="long Bybit demo TP/SL must bracket entry price"):
        client.set_full_position_protection(
            BybitDemoProtectionRequest(
                symbol="BTCUSDT",
                side="Buy",
                average_entry_price=Decimal("100000"),
                take_profit_price=Decimal("99000"),
                stop_loss_price=Decimal("98000"),
            )
        )


def test_short_demo_protection_requires_inverse_bracketing() -> None:
    request = BybitDemoProtectionRequest(
        symbol="ETHUSDT",
        side="Sell",
        average_entry_price=Decimal("4500"),
        take_profit_price=Decimal("4425"),
        stop_loss_price=Decimal("4530"),
    )

    request.validate()
