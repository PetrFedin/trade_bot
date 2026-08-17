import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

import pytest

from app.execution.bybit_demo_account_reader import BybitDemoAccountingClient


class _FakeTransport:
    def __init__(self, pages: list[Mapping[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.calls.append((path, query_string, dict(headers)))
        return self.pages.pop(0)


def _page(rows: list[dict[str, str]], cursor: str = "") -> dict[str, object]:
    return {
        "retCode": 0,
        "result": {
            "list": rows,
            "nextPageCursor": cursor,
        },
    }


def test_closed_pnl_reader_signs_exact_demo_get_contract() -> None:
    transport = _FakeTransport([_page([{"symbol": "BTCUSDT", "closedPnl": "1"}])])
    client = BybitDemoAccountingClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1234567890,
        recv_window_ms=5000,
    )

    rows = client.get_closed_pnl(symbol="BTCUSDT", limit=50)

    assert rows == ({"symbol": "BTCUSDT", "closedPnl": "1"},)
    assert client.host == "api-demo.bybit.com"
    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False
    path, query, headers = transport.calls[0]
    assert path == "/v5/position/closed-pnl"
    assert query == "category=linear&limit=50&symbol=BTCUSDT"
    payload = "1234567890" + "key" + "5000" + query
    expected = hmac.new(
        b"secret",
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-BAPI-SIGN"] == expected
    assert headers["X-BAPI-API-KEY"] == "key"
    assert headers["X-BAPI-TIMESTAMP"] == "1234567890"
    assert headers["X-BAPI-RECV-WINDOW"] == "5000"
    assert not hasattr(client, "place_order")
    assert not hasattr(client, "cancel_order")


def test_transaction_log_reader_paginates_with_cursor_and_detects_contract() -> None:
    transport = _FakeTransport(
        [
            _page([{"id": "1"}], cursor="next-1"),
            _page([{"id": "2"}]),
        ]
    )
    client = BybitDemoAccountingClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1234567890,
    )

    rows = client.get_transaction_log(
        symbol="BTCUSDT",
        start_time_ms=1000,
        end_time_ms=5000,
        limit=25,
    )

    assert rows == ({"id": "1"}, {"id": "2"})
    first_path, first_query, _headers = transport.calls[0]
    second_path, second_query, _headers = transport.calls[1]
    assert first_path == second_path == "/v5/account/transaction-log"
    assert "cursor=" not in first_query
    assert "cursor=next-1" in second_query
    assert "startTime=1000" in first_query
    assert "endTime=5000" in first_query


def test_accounting_reader_fails_on_cursor_loop() -> None:
    transport = _FakeTransport(
        [
            _page([], cursor="same"),
            _page([], cursor="same"),
        ]
    )
    client = BybitDemoAccountingClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
        clock_ms=lambda: 1,
    )

    with pytest.raises(RuntimeError, match="cursor loop"):
        client.get_closed_pnl(symbol="BTCUSDT")


def test_accounting_reader_rejects_non_normalized_symbol_and_bad_range() -> None:
    client = BybitDemoAccountingClient(
        api_key="key",
        api_secret="secret",
        transport=_FakeTransport([]),
    )
    with pytest.raises(ValueError):
        client.get_closed_pnl(symbol="btcUSDT")
    with pytest.raises(ValueError):
        client.get_transaction_log(
            symbol="BTCUSDT",
            start_time_ms=5000,
            end_time_ms=1000,
        )
