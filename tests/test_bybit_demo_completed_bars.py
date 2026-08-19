from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.marketdata.bybit_demo_completed_bars import (
    BybitDemoCompletedBarClient,
    BybitDemoKlineHttpJson,
)

_INTERVAL_MS = 5 * 60 * 1000


def _row(start_ms: int, *, close: str = "100") -> list[str]:
    return [
        str(start_ms),
        "100",
        "101",
        "99",
        close,
        "10",
        "1000",
    ]


def _payload(rows: list[list[str]]) -> dict[str, object]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "linear",
            "symbol": "BTCUSDT",
            "list": rows,
        },
        "time": 1_000_000,
    }


def test_completed_bar_reader_excludes_current_incomplete_bucket() -> None:
    observed: dict[str, object] = {}
    start = 10 * _INTERVAL_MS
    current_bucket = start + 3 * _INTERVAL_MS

    def transport(url: str, headers: dict[str, str]) -> BybitDemoKlineHttpJson:
        observed["url"] = url
        observed["headers"] = headers
        return BybitDemoKlineHttpJson(
            200,
            {},
            _payload(
                [
                    _row(current_bucket),
                    _row(start + 2 * _INTERVAL_MS),
                    _row(start + _INTERVAL_MS),
                    _row(start),
                ]
            ),
        )

    client = BybitDemoCompletedBarClient(transport=transport)
    bars = client.fetch_completed_range(
        symbol="BTCUSDT",
        start_ms=start,
        now_ms=current_bucket + 1_000,
        interval="5",
    )

    assert [int(bar.start_time.timestamp() * 1000) for bar in bars] == [
        start,
        start + _INTERVAL_MS,
        start + 2 * _INTERVAL_MS,
    ]
    parsed = urlsplit(str(observed["url"]))
    assert parsed.scheme == "https"
    assert parsed.hostname == "api-demo.bybit.com"
    assert parsed.path == "/v5/market/kline"
    query = parse_qs(parsed.query)
    assert query["symbol"] == ["BTCUSDT"]
    assert query["interval"] == ["5"]
    assert int(query["end"][0]) == current_bucket - 1
    assert int(query["limit"][0]) == 3
    assert observed["headers"] == {"Accept": "application/json"}
    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False


def test_completed_bar_reader_sorts_reverse_api_rows_into_time_order() -> None:
    start = 20 * _INTERVAL_MS

    def transport(_: str, __: dict[str, str]) -> BybitDemoKlineHttpJson:
        return BybitDemoKlineHttpJson(
            200,
            {},
            _payload([_row(start + _INTERVAL_MS), _row(start)]),
        )

    bars = BybitDemoCompletedBarClient(transport=transport).fetch_completed_range(
        symbol="BTCUSDT",
        start_ms=start,
        now_ms=start + 2 * _INTERVAL_MS + 1,
    )

    assert [int(bar.start_time.timestamp() * 1000) for bar in bars] == [
        start,
        start + _INTERVAL_MS,
    ]


def test_completed_bar_reader_fails_closed_on_missing_bar() -> None:
    start = 30 * _INTERVAL_MS

    def transport(_: str, __: dict[str, str]) -> BybitDemoKlineHttpJson:
        return BybitDemoKlineHttpJson(
            200,
            {},
            _payload([_row(start + 2 * _INTERVAL_MS), _row(start)]),
        )

    with pytest.raises(ValueError, match="not contiguous"):
        BybitDemoCompletedBarClient(transport=transport).fetch_completed_range(
            symbol="BTCUSDT",
            start_ms=start,
            now_ms=start + 3 * _INTERVAL_MS + 1,
        )


def test_completed_bar_reader_rejects_unaligned_start() -> None:
    client = BybitDemoCompletedBarClient(
        transport=lambda *_: pytest.fail("transport must not run")
    )

    with pytest.raises(ValueError, match="aligned"):
        client.fetch_completed_range(
            symbol="BTCUSDT",
            start_ms=123,
            now_ms=10 * _INTERVAL_MS,
        )


def test_completed_bar_reader_returns_empty_before_first_bar_completes() -> None:
    start = 40 * _INTERVAL_MS
    client = BybitDemoCompletedBarClient(
        transport=lambda *_: pytest.fail("transport must not run")
    )

    bars = client.fetch_completed_range(
        symbol="BTCUSDT",
        start_ms=start,
        now_ms=start + _INTERVAL_MS - 1,
    )

    assert bars == ()


def test_completed_bar_reader_rejects_ranges_above_single_page() -> None:
    start = 50 * _INTERVAL_MS
    client = BybitDemoCompletedBarClient(
        transport=lambda *_: pytest.fail("transport must not run")
    )

    with pytest.raises(ValueError, match="exceeds one deterministic kline page"):
        client.fetch_completed_range(
            symbol="BTCUSDT",
            start_ms=start,
            now_ms=start + 1001 * _INTERVAL_MS + 1,
        )
