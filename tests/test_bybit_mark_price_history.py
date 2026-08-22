from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from app.marketdata.bybit_mark_price_history import (
    BybitMarkPriceHistoryClient,
    BybitMarkPriceHttpJson,
)

_START = 1_700_000_000_000
_HOUR = 3_600_000
_END = _START + 4 * _HOUR


class _Transport:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str]] = []

    def __call__(
        self,
        host: str,
        path: str,
        metadata: Mapping[str, str],
    ) -> BybitMarkPriceHttpJson:
        self.calls.append((host, path, metadata.get("query", "")))
        return BybitMarkPriceHttpJson(
            status_code=200,
            headers={},
            payload=self.responses.pop(0),
        )


def _ok(rows: list[list[str]], *, symbol: str = "BTCUSDT") -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "linear",
            "symbol": symbol,
            "list": rows,
        },
    }


def _row(timestamp: int, price: str) -> list[str]:
    value = Decimal(price)
    return [
        str(timestamp),
        price,
        str(value + Decimal("10")),
        str(value - Decimal("10")),
        str(value + Decimal("1")),
    ]


def test_mark_price_history_parses_reverse_page_and_supports_exact_settlement_lookup() -> None:
    transport = _Transport(
        [
            _ok(
                [
                    _row(_START + 3 * _HOUR, "103000"),
                    _row(_START + 2 * _HOUR, "102000"),
                    _row(_START + _HOUR, "101000"),
                ]
            )
        ]
    )
    client = BybitMarkPriceHistoryClient(
        host="api.bybit.eu",
        transport=transport,
    )

    history = client.fetch_history(
        symbol="BTCUSDT",
        start_ms=_START,
        end_ms=_END,
        interval="60",
    )

    assert [point.start_time_ms for point in history.points] == [
        _START + _HOUR,
        _START + 2 * _HOUR,
        _START + 3 * _HOUR,
    ]
    assert history.open_price_at(_START + 2 * _HOUR) == Decimal("102000")
    assert history.open_price_at(_START + 2 * _HOUR + 1) is None
    assert history.host == "api.bybit.eu"
    assert history.live_mainnet_order_routing_allowed is False
    assert history.order_writes_supported is False
    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False
    assert not hasattr(client, "place_order")
    assert transport.calls[0][1] == "/v5/market/mark-price-kline"
    assert "interval=60" in transport.calls[0][2]


def test_mark_price_history_pages_backwards_without_duplicate_conflict() -> None:
    rows_first = [
        _row(_START + (1001 - index) * _HOUR, "100000")
        for index in range(1000)
    ]
    oldest = _START + 2 * _HOUR
    assert int(rows_first[-1][0]) == oldest
    transport = _Transport(
        [
            _ok(rows_first),
            _ok([_row(_START + _HOUR, "99000"), _row(_START, "98000")]),
        ]
    )
    client = BybitMarkPriceHistoryClient(
        transport=transport,
        maximum_pages=3,
    )
    history = client.fetch_history(
        symbol="BTCUSDT",
        start_ms=_START,
        end_ms=_START + 1001 * _HOUR,
        interval="60",
    )
    assert history.request_count == 2
    assert history.points[0].start_time_ms == _START
    assert history.points[-1].start_time_ms == _START + 1001 * _HOUR
    assert len(history.points) == 1002


def test_mark_price_identity_and_malformed_ohlc_fail_closed() -> None:
    wrong_identity = _Transport([_ok([], symbol="ETHUSDT")])
    with pytest.raises(RuntimeError, match="identity"):
        BybitMarkPriceHistoryClient(transport=wrong_identity).fetch_history(
            symbol="BTCUSDT",
            start_ms=_START,
            end_ms=_END,
        )

    malformed = _Transport(
        [
            _ok(
                [
                    [
                        str(_START),
                        "100",
                        "99",
                        "98",
                        "100",
                    ]
                ]
            )
        ]
    )
    with pytest.raises(ValueError, match="high"):
        BybitMarkPriceHistoryClient(transport=malformed).fetch_history(
            symbol="BTCUSDT",
            start_ms=_START,
            end_ms=_END,
        )


def test_invalid_host_symbol_interval_and_nonzero_ret_code_fail_closed() -> None:
    transport = _Transport([])
    with pytest.raises(ValueError, match="allowlist"):
        BybitMarkPriceHistoryClient(host="example.com", transport=transport)
    client = BybitMarkPriceHistoryClient(transport=transport)
    with pytest.raises(ValueError, match="normalized USDT"):
        client.fetch_history(symbol="btcUSDT", start_ms=_START, end_ms=_END)
    with pytest.raises(ValueError, match="unsupported"):
        client.fetch_history(
            symbol="BTCUSDT",
            start_ms=_START,
            end_ms=_END,
            interval="D",
        )
    assert transport.calls == []

    failed = _Transport(
        [
            {
                "retCode": 10001,
                "retMsg": "bad request",
                "result": {},
            }
        ]
    )
    with pytest.raises(RuntimeError, match="retCode"):
        BybitMarkPriceHistoryClient(transport=failed).fetch_history(
            symbol="BTCUSDT",
            start_ms=_START,
            end_ms=_END,
        )
