from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.marketdata.bybit_v5 import (
    BybitHttpJson,
    BybitKlineRequest,
    BybitPublicKlineClient,
)


def test_public_kline_client_uses_one_audited_regional_host() -> None:
    urls: list[str] = []

    def transport(url: str, _headers: dict[str, str]) -> BybitHttpJson:
        urls.append(url)
        parsed = urlsplit(url)
        symbol = parse_qs(parsed.query)["symbol"][0]
        start_ms = int(parse_qs(parsed.query)["start"][0])
        return BybitHttpJson(
            status_code=200,
            headers={},
            payload={
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "symbol": symbol,
                    "list": [
                        [
                            str(start_ms),
                            "100",
                            "101",
                            "99",
                            "100.5",
                            "10",
                            "1000",
                        ]
                    ],
                },
            },
        )

    client = BybitPublicKlineClient(host="api.bybit.eu", transport=transport)
    acquisition = client.fetch(
        BybitKlineRequest(
            symbols=("BTCUSDT", "ETHUSDT"),
            start_ms=1_000_000,
            end_ms=1_300_000,
            interval="5",
        )
    )

    assert client.host == "api.bybit.eu"
    assert acquisition.symbols == ("BTCUSDT", "ETHUSDT")
    assert len(urls) == 2
    assert all(urlsplit(url).hostname == "api.bybit.eu" for url in urls)
    assert client.order_writes_supported is False
    assert client.live_mainnet_order_routing_allowed is False


def test_public_kline_client_rejects_non_allowlisted_or_noncanonical_host() -> None:
    with pytest.raises(ValueError, match="audited mainnet allowlist"):
        BybitPublicKlineClient(host="evil.example")
    with pytest.raises(ValueError, match="audited mainnet allowlist"):
        BybitPublicKlineClient(host="API.BYBIT.EU")
