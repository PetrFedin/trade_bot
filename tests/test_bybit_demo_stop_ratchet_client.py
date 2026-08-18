from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.execution.bybit_demo import BybitDemoHttpJson
from app.execution.bybit_demo_stop_ratchet_client import (
    BybitDemoStopRatchetClient,
    BybitDemoStopRatchetRequest,
)


def _client(observed: dict[str, object]) -> BybitDemoStopRatchetClient:
    def transport(
        method: str,
        url: str,
        headers: dict[str, str],
        body: str | None,
    ) -> BybitDemoHttpJson:
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = headers
        observed["body"] = body
        return BybitDemoHttpJson(
            status_code=200,
            headers={},
            payload={"retCode": 0, "retMsg": "OK", "result": {}, "time": 1_000_000},
        )

    return BybitDemoStopRatchetClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=transport,
        clock_ms=lambda: 1_000_000,
    )


def test_stop_ratchet_posts_only_stop_loss_and_preserves_other_protection_fields() -> None:
    observed: dict[str, object] = {}
    client = _client(observed)
    request = BybitDemoStopRatchetRequest(
        symbol="BTCUSDT",
        side="Buy",
        previous_stop_loss_price=Decimal("95"),
        new_stop_loss_price=Decimal("100.2"),
        current_last_price=Decimal("104"),
    )

    ack = client.ratchet_position_stop_loss(request)

    assert observed["method"] == "POST"
    assert observed["url"] == "https://api-demo.bybit.com/v5/position/trading-stop"
    body = json.loads(str(observed["body"]))
    assert body == {
        "category": "linear",
        "positionIdx": 0,
        "slTriggerBy": "LastPrice",
        "stopLoss": "100.2",
        "symbol": "BTCUSDT",
        "tpslMode": "Full",
    }
    assert "takeProfit" not in body
    assert "trailingStop" not in body
    assert "activePrice" not in body
    assert ack.stop_loss_price == Decimal("100.2")
    assert ack.live_mainnet_order is False
    assert client.live_mainnet_order_routing_allowed is False
    assert client.stop_ratchet_write_supported is True


def test_short_stop_ratchet_requires_lower_stop_still_above_current_market() -> None:
    request = BybitDemoStopRatchetRequest(
        symbol="BTCUSDT",
        side="Sell",
        previous_stop_loss_price=Decimal("105"),
        new_stop_loss_price=Decimal("98.3"),
        current_last_price=Decimal("96"),
    )

    request.validate()


def test_stop_ratchet_rejects_widening_or_already_crossed_stop() -> None:
    with pytest.raises(ValueError, match="previous < new < current"):
        BybitDemoStopRatchetRequest(
            symbol="BTCUSDT",
            side="Buy",
            previous_stop_loss_price=Decimal("95"),
            new_stop_loss_price=Decimal("94"),
            current_last_price=Decimal("104"),
        ).validate()

    with pytest.raises(ValueError, match="previous < new < current"):
        BybitDemoStopRatchetRequest(
            symbol="BTCUSDT",
            side="Buy",
            previous_stop_loss_price=Decimal("95"),
            new_stop_loss_price=Decimal("104"),
            current_last_price=Decimal("104"),
        ).validate()

    with pytest.raises(ValueError, match="current < new < previous"):
        BybitDemoStopRatchetRequest(
            symbol="BTCUSDT",
            side="Sell",
            previous_stop_loss_price=Decimal("105"),
            new_stop_loss_price=Decimal("106"),
            current_last_price=Decimal("96"),
        ).validate()
