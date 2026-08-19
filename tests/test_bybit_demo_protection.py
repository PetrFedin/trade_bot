import json
from decimal import Decimal

import pytest

from app.execution.bybit_demo import (
    BybitDemoHttpJson,
    BybitDemoOrderClient,
    BybitDemoProtectionRequest,
    BybitDemoRunnerProtectionRequest,
)


def _client(captured: dict[str, object]) -> BybitDemoOrderClient:
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

    return BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=transport,
        clock_ms=lambda: 1_700_000_000_000,
    )


def test_demo_gateway_sets_full_position_tp_sl_on_demo_endpoint() -> None:
    captured: dict[str, object] = {}
    client = _client(captured)
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


def test_demo_gateway_sets_uncapped_runner_without_take_profit() -> None:
    captured: dict[str, object] = {}
    client = _client(captured)
    acknowledgement = client.set_open_ended_position_protection(
        BybitDemoRunnerProtectionRequest(
            symbol="BTCUSDT",
            side="Buy",
            average_entry_price=Decimal("100000"),
            stop_loss_price=Decimal("99600"),
            trailing_stop_distance=Decimal("250"),
            trailing_active_price=Decimal("101250"),
        )
    )

    payload = json.loads(str(captured["body"]))
    assert captured["url"] == "https://api-demo.bybit.com/v5/position/trading-stop"
    assert payload == {
        "activePrice": "101250",
        "category": "linear",
        "positionIdx": 0,
        "slTriggerBy": "LastPrice",
        "stopLoss": "99600",
        "symbol": "BTCUSDT",
        "trailingStop": "250",
        "tpslMode": "Full",
    }
    assert "takeProfit" not in payload
    assert acknowledgement.trailing_active_price == Decimal("101250")
    assert acknowledgement.trailing_stop_distance == Decimal("250")
    assert acknowledgement.live_mainnet_order is False


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


def test_runner_rejects_activation_on_the_wrong_side_of_entry() -> None:
    request = BybitDemoRunnerProtectionRequest(
        symbol="BTCUSDT",
        side="Buy",
        average_entry_price=Decimal("100000"),
        stop_loss_price=Decimal("99500"),
        trailing_stop_distance=Decimal("250"),
        trailing_active_price=Decimal("99900"),
    )

    with pytest.raises(ValueError, match="stop < entry < activation"):
        request.validate()


def test_short_demo_protection_requires_inverse_bracketing() -> None:
    request = BybitDemoProtectionRequest(
        symbol="ETHUSDT",
        side="Sell",
        average_entry_price=Decimal("4500"),
        take_profit_price=Decimal("4425"),
        stop_loss_price=Decimal("4530"),
    )

    request.validate()


def test_short_runner_requires_inverse_bracketing() -> None:
    request = BybitDemoRunnerProtectionRequest(
        symbol="ETHUSDT",
        side="Sell",
        average_entry_price=Decimal("4500"),
        stop_loss_price=Decimal("4530"),
        trailing_stop_distance=Decimal("5"),
        trailing_active_price=Decimal("4425"),
    )

    request.validate()
