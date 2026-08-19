from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import parse_qs, urlsplit

import pytest

from app.execution.bybit_demo import BybitDemoHttpJson
from app.execution.bybit_demo_broker_truth import BybitDemoBrokerTruthClient


def _transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: str | None,
) -> BybitDemoHttpJson:
    assert method == "GET"
    assert body is None
    assert headers["X-BAPI-API-KEY"] == "key"
    parsed = urlsplit(url)
    assert parsed.hostname == "api-demo.bybit.com"
    assert parsed.path == "/v5/order/realtime"
    query = parse_qs(parsed.query)
    assert query == {
        "category": ["linear"],
        "settleCoin": ["USDT"],
        "openOnly": ["0"],
        "limit": ["50"],
    }
    return BybitDemoHttpJson(
        status_code=200,
        headers={},
        payload={
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "orderStatus": "New",
                        "orderLinkId": "ASTRA-DEMO-OPEN-1",
                    }
                ]
            },
        },
    )


def test_broker_truth_client_reads_only_current_open_orders() -> None:
    client = BybitDemoBrokerTruthClient(
        api_key="key",
        api_secret="secret",
        transport=_transport,
        clock_ms=lambda: 1_700_000_000_000,
    )

    rows = client.get_open_orders()

    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert client.live_mainnet_order_routing_allowed is False


@pytest.mark.parametrize("limit", [0, 51])
def test_broker_truth_client_rejects_incomplete_or_invalid_limits(limit: int) -> None:
    client = BybitDemoBrokerTruthClient(
        api_key="key",
        api_secret="secret",
        transport=_transport,
    )

    with pytest.raises(ValueError, match="limit"):
        client.get_open_orders(limit=limit)


def test_broker_truth_client_rejects_non_usdt_settlement() -> None:
    client = BybitDemoBrokerTruthClient(
        api_key="key",
        api_secret="secret",
        transport=_transport,
    )

    with pytest.raises(ValueError, match="USDT"):
        client.get_open_orders(settle_coin="USDC")


def test_broker_truth_client_rejects_malformed_order_payload() -> None:
    def malformed(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: str | None,
    ) -> BybitDemoHttpJson:
        del method, url, headers, body
        return BybitDemoHttpJson(
            status_code=200,
            headers={},
            payload={"retCode": 0, "retMsg": "OK", "result": {"list": ["bad"]}},
        )

    client = BybitDemoBrokerTruthClient(
        api_key="key",
        api_secret="secret",
        transport=malformed,
    )

    with pytest.raises(ValueError, match="missing list"):
        client.get_open_orders()
