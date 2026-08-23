from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import pytest

from app.execution.bybit_mainnet_funding_ledger import BybitMainnetFundingLedgerClient

_START = datetime(2026, 8, 1, tzinfo=UTC)


class _FakeTransport:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any]:
        self.calls.append((path, query_string, dict(headers)))
        return self.responses.pop(0)


def _ok_result(rows: list[dict[str, Any]], *, cursor: str = "") -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "list": rows,
            "nextPageCursor": cursor,
        },
    }


def _row(
    *,
    transaction_id: str,
    symbol: str,
    moment: datetime,
    funding: str,
    cash_flow: str = "0",
) -> dict[str, Any]:
    return {
        "transactionId": transaction_id,
        "symbol": symbol,
        "transactionTime": str(int(moment.timestamp() * 1000)),
        "funding": funding,
        "cashFlow": cash_flow,
        "type": "SETTLEMENT",
    }


def test_funding_ledger_pages_and_splits_windows_without_order_surface() -> None:
    transport = _FakeTransport(
        [
            _ok_result(
                [
                    _row(
                        transaction_id="tx-a",
                        symbol="BTCUSDT",
                        moment=_START + timedelta(hours=8),
                        funding="-1.25",
                    )
                ],
                cursor="page-2",
            ),
            _ok_result(
                [
                    _row(
                        transaction_id="tx-b",
                        symbol="ETHUSDT",
                        moment=_START + timedelta(days=3, hours=16),
                        funding="0.75",
                    )
                ]
            ),
            _ok_result(
                [
                    _row(
                        transaction_id="tx-c",
                        symbol="SOLUSDT",
                        moment=_START + timedelta(days=7, hours=8),
                        funding="0",
                    )
                ]
            ),
        ]
    )
    client = BybitMainnetFundingLedgerClient(
        api_key="key",
        api_secret="secret",
        host="api.bybit.eu",
        transport=transport,
        clock_ms=lambda: 1234567890,
    )

    acquisition = client.fetch_broker_funding_ledger(
        start_at=_START,
        end_exclusive_at=_START + timedelta(days=8),
    )

    assert acquisition.request_count == 3
    assert acquisition.window_count == 2
    assert [entry.transaction_id for entry in acquisition.entries] == [
        "tx-a",
        "tx-b",
        "tx-c",
    ]
    assert [entry.direction for entry in acquisition.entries] == [
        "PAID",
        "RECEIVED",
        "ZERO",
    ]
    assert acquisition.total_funding_usdt == Decimal("-0.50")
    assert acquisition.received_funding_usdt == Decimal("0.75")
    assert acquisition.paid_funding_usdt == Decimal("1.25")
    assert acquisition.read_only is True
    assert acquisition.order_writes_supported is False
    assert acquisition.bybit_live_order_routing_allowed is False
    assert acquisition.public_reconstruction_reconciled is False
    assert client.order_writes_supported is False
    assert client.live_mainnet_order_routing_allowed is False
    assert not hasattr(client, "create_order")
    assert not hasattr(client, "amend_order")
    assert not hasattr(client, "cancel_order")

    assert [call[0] for call in transport.calls] == [
        "/v5/account/transaction-log",
        "/v5/account/transaction-log",
        "/v5/account/transaction-log",
    ]
    first = parse_qs(transport.calls[0][1])
    second = parse_qs(transport.calls[1][1])
    third = parse_qs(transport.calls[2][1])
    first_window_end_ms = int((_START + timedelta(days=7)).timestamp() * 1000) - 1
    final_end_ms = int((_START + timedelta(days=8)).timestamp() * 1000) - 1
    assert first["accountType"] == ["UNIFIED"]
    assert first["category"] == ["linear"]
    assert first["currency"] == ["USDT"]
    assert first["type"] == ["SETTLEMENT"]
    assert first["limit"] == ["50"]
    assert first["startTime"] == [str(int(_START.timestamp() * 1000))]
    assert first["endTime"] == [str(first_window_end_ms)]
    assert "cursor" not in first
    assert second["cursor"] == ["page-2"]
    assert second["startTime"] == first["startTime"]
    assert second["endTime"] == first["endTime"]
    assert third["startTime"] == [
        str(int((_START + timedelta(days=7)).timestamp() * 1000))
    ]
    assert third["endTime"] == [str(final_end_ms)]


def test_funding_ledger_rejects_conflicting_duplicate_transaction() -> None:
    moment = _START + timedelta(hours=8)
    transport = _FakeTransport(
        [
            _ok_result(
                [_row(transaction_id="same", symbol="BTCUSDT", moment=moment, funding="1")],
                cursor="next",
            ),
            _ok_result(
                [_row(transaction_id="same", symbol="BTCUSDT", moment=moment, funding="2")]
            ),
        ]
    )
    client = BybitMainnetFundingLedgerClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
    )

    with pytest.raises(ValueError, match="duplicate transaction conflicts"):
        client.fetch_broker_funding_ledger(
            start_at=_START,
            end_exclusive_at=_START + timedelta(days=1),
        )


def test_funding_ledger_rejects_history_beyond_two_year_contract() -> None:
    client = BybitMainnetFundingLedgerClient(
        api_key="key",
        api_secret="secret",
        transport=_FakeTransport([]),
    )

    with pytest.raises(ValueError, match="730 days"):
        client.fetch_broker_funding_ledger(
            start_at=_START,
            end_exclusive_at=_START + timedelta(days=731),
        )


def test_funding_ledger_rejects_cursor_that_does_not_advance() -> None:
    transport = _FakeTransport(
        [
            _ok_result([], cursor="same"),
            _ok_result([], cursor="same"),
        ]
    )
    client = BybitMainnetFundingLedgerClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
    )

    with pytest.raises(ValueError, match="cursor did not advance"):
        client.fetch_broker_funding_ledger(
            start_at=_START,
            end_exclusive_at=_START + timedelta(days=1),
        )
