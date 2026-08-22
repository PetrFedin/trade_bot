from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from app.marketdata.bybit_derivatives_history import (
    BybitDerivativesHttpJson,
    BybitHistoricalDerivativesClient,
)

_START = 1_700_000_000_000
_END = _START + 3_600_000


class _Transport:
    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str]] = []

    def __call__(
        self,
        host: str,
        path: str,
        metadata: Mapping[str, str],
    ) -> BybitDerivativesHttpJson:
        self.calls.append((host, path, metadata.get("query", "")))
        return BybitDerivativesHttpJson(
            status_code=200,
            headers={},
            payload=self.responses.pop(0),
        )


def _ok(result: Mapping[str, Any]) -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK", "result": dict(result)}


def test_history_fetches_cursor_series_and_funding_with_zero_write_surface() -> None:
    transport = _Transport(
        [
            _ok(
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "list": [
                        {
                            "openInterest": "10",
                            "singleOpenInterest": "5",
                            "timestamp": str(_START + 3_000_000),
                        }
                    ],
                    "nextPageCursor": "oi-next",
                }
            ),
            _ok(
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "list": [
                        {
                            "openInterest": "9",
                            "singleOpenInterest": "4.5",
                            "timestamp": str(_START + 1_000_000),
                        }
                    ],
                    "nextPageCursor": "",
                }
            ),
            _ok(
                {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "buyRatio": "0.55",
                            "sellRatio": "0.45",
                            "timestamp": str(_START + 3_000_000),
                        }
                    ],
                    "nextPageCursor": "ratio-next",
                }
            ),
            _ok(
                {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "buyRatio": "0.52",
                            "sellRatio": "0.48",
                            "timestamp": str(_START + 1_000_000),
                        }
                    ],
                    "nextPageCursor": "",
                }
            ),
            _ok(
                {
                    "category": "linear",
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "fundingRate": "0.0001",
                            "fundingRateTimestamp": str(_START + 2_000_000),
                        }
                    ],
                }
            ),
        ]
    )
    client = BybitHistoricalDerivativesClient(
        host="api.bybit.eu",
        transport=transport,
    )

    history = client.fetch_history(
        symbol="BTCUSDT",
        start_ms=_START,
        end_ms=_END,
        interval="5min",
    )

    assert [point.open_interest for point in history.open_interest] == [
        Decimal("9"),
        Decimal("10"),
    ]
    assert [point.buy_ratio for point in history.account_ratio] == [
        Decimal("0.52"),
        Decimal("0.55"),
    ]
    assert [point.funding_rate for point in history.funding] == [
        Decimal("0.0001")
    ]
    assert history.request_count == 5
    assert history.host == "api.bybit.eu"
    assert history.live_mainnet_order_routing_allowed is False
    assert history.order_writes_supported is False
    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False
    assert not hasattr(client, "place_order")
    assert not hasattr(client, "cancel_order")
    assert transport.calls[0][0] == "api.bybit.eu"
    assert transport.calls[0][1] == "/v5/market/open-interest"
    assert "cursor=oi-next" in transport.calls[1][2]
    assert transport.calls[2][1] == "/v5/market/account-ratio"
    assert "cursor=ratio-next" in transport.calls[3][2]
    assert transport.calls[4][1] == "/v5/market/funding/history"


def test_funding_uses_bounded_daily_windows_for_multi_day_range() -> None:
    end = _START + 2 * 86_400_000 + 1_000
    transport = _Transport(
        [
            _ok(
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "list": [],
                    "nextPageCursor": "",
                }
            ),
            _ok({"list": [], "nextPageCursor": ""}),
            _ok({"category": "linear", "list": []}),
            _ok({"category": "linear", "list": []}),
            _ok({"category": "linear", "list": []}),
        ]
    )
    history = BybitHistoricalDerivativesClient(transport=transport).fetch_history(
        symbol="BTCUSDT",
        start_ms=_START,
        end_ms=end,
        interval="1h",
    )
    assert history.request_count == 5
    funding_calls = [
        call
        for call in transport.calls
        if call[1] == "/v5/market/funding/history"
    ]
    assert len(funding_calls) == 3


def test_repeated_cursor_and_conflicting_duplicate_timestamp_fail_closed() -> None:
    repeated = _Transport(
        [
            _ok(
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "list": [],
                    "nextPageCursor": "same",
                }
            ),
            _ok(
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "list": [],
                    "nextPageCursor": "same",
                }
            ),
        ]
    )
    client = BybitHistoricalDerivativesClient(transport=repeated)
    with pytest.raises(RuntimeError, match="cursor"):
        client.fetch_open_interest(
            symbol="BTCUSDT",
            start_ms=_START,
            end_ms=_END,
            interval="5min",
        )

    duplicate = _Transport(
        [
            _ok(
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "list": [
                        {
                            "openInterest": "10",
                            "timestamp": str(_START + 1_000),
                        },
                        {
                            "openInterest": "11",
                            "timestamp": str(_START + 1_000),
                        },
                    ],
                    "nextPageCursor": "",
                }
            )
        ]
    )
    with pytest.raises(RuntimeError, match="conflicting duplicate"):
        BybitHistoricalDerivativesClient(transport=duplicate).fetch_open_interest(
            symbol="BTCUSDT",
            start_ms=_START,
            end_ms=_END,
            interval="5min",
        )


def test_ratio_reconciliation_and_nonfinite_values_fail_closed() -> None:
    bad_ratio = _Transport(
        [
            _ok(
                {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "buyRatio": "0.8",
                            "sellRatio": "0.8",
                            "timestamp": str(_START + 1_000),
                        }
                    ],
                    "nextPageCursor": "",
                }
            )
        ]
    )
    with pytest.raises(ValueError, match="reconcile"):
        BybitHistoricalDerivativesClient(transport=bad_ratio).fetch_account_ratio(
            symbol="BTCUSDT",
            start_ms=_START,
            end_ms=_END,
            interval="5min",
        )

    bad_oi = _Transport(
        [
            _ok(
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "list": [
                        {
                            "openInterest": "NaN",
                            "timestamp": str(_START + 1_000),
                        }
                    ],
                    "nextPageCursor": "",
                }
            )
        ]
    )
    with pytest.raises(RuntimeError, match="invalid openInterest"):
        BybitHistoricalDerivativesClient(transport=bad_oi).fetch_open_interest(
            symbol="BTCUSDT",
            start_ms=_START,
            end_ms=_END,
            interval="5min",
        )


def test_invalid_symbol_interval_and_host_are_rejected_before_transport() -> None:
    transport = _Transport([])
    client = BybitHistoricalDerivativesClient(transport=transport)
    with pytest.raises(ValueError, match="normalized USDT"):
        client.fetch_open_interest(
            symbol="btcUSDT",
            start_ms=_START,
            end_ms=_END,
            interval="5min",
        )
    with pytest.raises(ValueError, match="unsupported"):
        client.fetch_open_interest(
            symbol="BTCUSDT",
            start_ms=_START,
            end_ms=_END,
            interval="2h",
        )
    with pytest.raises(ValueError, match="allowlist"):
        BybitHistoricalDerivativesClient(host="example.com", transport=transport)
    assert transport.calls == []
