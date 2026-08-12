from __future__ import annotations

import json
from decimal import Decimal

from app.execution.bybit_demo import BybitDemoHttpJson, BybitDemoOrderClient


def _client(captured: dict[str, object], payload: dict[str, object]) -> BybitDemoOrderClient:
    def transport(
        method: str,
        url: str,
        headers: dict[str, str],
        body: str | None,
    ) -> BybitDemoHttpJson:
        captured.update(method=method, url=url, headers=headers, body=body)
        return BybitDemoHttpJson(status_code=200, headers={}, payload=payload)

    return BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=transport,
        clock_ms=lambda: 1_700_000_000_000,
    )


def test_get_fee_rate_reads_account_specific_taker_and_maker_rates() -> None:
    captured: dict[str, object] = {}
    client = _client(
        captured,
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "takerFeeRate": "0.00055",
                        "makerFeeRate": "0.00010",
                    }
                ]
            },
        },
    )

    fee = client.get_fee_rate(symbol="BTCUSDT")

    assert captured["method"] == "GET"
    assert captured["url"] == "https://api-demo.bybit.com/v5/account/fee-rate?category=linear&symbol=BTCUSDT"
    assert fee.symbol == "BTCUSDT"
    assert fee.taker_fee_rate == Decimal("0.00055")
    assert fee.maker_fee_rate == Decimal("0.00010")


def test_fee_rate_response_requires_symbol_match_and_positive_rates() -> None:
    captured: dict[str, object] = {}
    client = _client(
        captured,
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "symbol": "ETHUSDT",
                        "takerFeeRate": "0.0006",
                        "makerFeeRate": "0.0001",
                    }
                ]
            },
        },
    )

    try:
        client.get_fee_rate(symbol="BTCUSDT")
    except ValueError as exc:
        assert "fee rate response missing BTCUSDT" in str(exc)
    else:
        raise AssertionError("expected symbol mismatch to fail closed")
