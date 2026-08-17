from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping

import pytest

from app.marketdata.bybit_funding import BybitFundingHistoryClient


class _FakeTransport:
    def __init__(self, payloads: list[Mapping[str, Any]]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, str]] = []

    def get_json(self, *, path: str, query_string: str) -> Mapping[str, Any]:
        self.calls.append((path, query_string))
        return self.payloads.pop(0)


def _payload(rows: list[dict[str, str]]) -> dict[str, object]:
    return {
        "retCode": 0,
        "result": {
            "list": rows,
        },
    }


def test_funding_history_uses_exact_public_v5_path_and_normalizes_order() -> None:
    transport = _FakeTransport(
        [
            _payload(
                [
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "0.0001",
                        "fundingRateTimestamp": "1785542400000",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "-0.0002",
                        "fundingRateTimestamp": "1785513600000",
                    },
                ]
            )
        ]
    )
    client = BybitFundingHistoryClient(transport=transport)

    history = client.fetch_history(
        symbol="BTCUSDT",
        start_time=datetime(2026, 8, 1, tzinfo=UTC),
        end_time=datetime(2026, 8, 2, tzinfo=UTC),
    )

    assert history.request_count == 1
    assert history.live_mainnet_order_routing_allowed is False
    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False
    assert transport.calls[0][0] == "/v5/market/funding/history"
    query = transport.calls[0][1]
    assert "category=linear" in query
    assert "symbol=BTCUSDT" in query
    assert "limit=200" in query
    assert [row.funding_rate for row in history.records] == [
        Decimal("-0.0002"),
        Decimal("0.0001"),
    ]


def test_multi_day_windows_dedupe_same_boundary_timestamp() -> None:
    duplicate = {
        "symbol": "BTCUSDT",
        "fundingRate": "0.0001",
        "fundingRateTimestamp": "1785628800000",
    }
    transport = _FakeTransport(
        [
            _payload([duplicate]),
            _payload([duplicate]),
        ]
    )
    client = BybitFundingHistoryClient(transport=transport)

    history = client.fetch_history(
        symbol="BTCUSDT",
        start_time=datetime(2026, 8, 1, tzinfo=UTC),
        end_time=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert history.request_count == 2
    assert len(history.records) == 1


def test_conflicting_duplicate_funding_timestamp_fails_closed() -> None:
    transport = _FakeTransport(
        [
            _payload(
                [
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "0.0001",
                        "fundingRateTimestamp": "1785628800000",
                    }
                ]
            ),
            _payload(
                [
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "0.0002",
                        "fundingRateTimestamp": "1785628800000",
                    }
                ]
            ),
        ]
    )
    client = BybitFundingHistoryClient(transport=transport)

    with pytest.raises(RuntimeError, match="conflicting"):
        client.fetch_history(
            symbol="BTCUSDT",
            start_time=datetime(2026, 8, 1, tzinfo=UTC),
            end_time=datetime(2026, 8, 3, tzinfo=UTC),
        )


def test_funding_history_rejects_non_normalized_symbol_and_naive_time() -> None:
    client = BybitFundingHistoryClient(transport=_FakeTransport([]))
    with pytest.raises(ValueError):
        client.fetch_history(
            symbol="btcusdt",
            start_time=datetime(2026, 8, 1, tzinfo=UTC),
            end_time=datetime(2026, 8, 2, tzinfo=UTC),
        )
    with pytest.raises(ValueError):
        client.fetch_history(
            symbol="BTCUSDT",
            start_time=datetime(2026, 8, 1),
            end_time=datetime(2026, 8, 2, tzinfo=UTC),
        )
