from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.bybit_demo import (
    BybitDemoHttpJson,
    BybitDemoOrderClient,
    BybitDemoOrderRequest,
)
from app.execution.bybit_demo_account_reader import (
    BybitDemoAccountingClient,
    BybitDemoAccountingHttpJson,
)
from app.execution.bybit_rest_policy import (
    BybitRestClockSkewError,
    BybitRestPolicy,
    BybitRestRateLimitError,
    BybitRestRequestError,
    BybitRestTransportError,
    parse_rate_limit_headers,
)


def _policy(*, attempts: int = 3) -> BybitRestPolicy:
    return BybitRestPolicy(
        request_timeout_seconds=1,
        read_max_attempts=attempts,
        read_backoff_initial_seconds=0.01,
        read_backoff_max_seconds=0.02,
    )


def _fee_success() -> BybitDemoHttpJson:
    return BybitDemoHttpJson(
        status_code=200,
        headers={"X-Bapi-Limit-Status": "99"},
        payload={
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "takerFeeRate": "0.00055",
                        "makerFeeRate": "0.0002",
                    }
                ]
            },
        },
    )


def test_safe_get_retries_transport_timeout_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    def _transport(_method, _url, _headers, _body):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("simulated timeout")
        return _fee_success()

    client = BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=_transport,
        clock_ms=lambda: 1_000,
        rest_policy=_policy(),
        sleep_fn=sleeps.append,
    )

    fee = client.get_fee_rate(symbol="BTCUSDT")

    assert calls == 2
    assert sleeps == [0.01]
    assert fee.taker_fee_rate == Decimal("0.00055")


def test_safe_get_retries_http_and_api_rate_limits_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    def _transport(_method, _url, _headers, _body):
        nonlocal calls
        calls += 1
        if calls == 1:
            return BybitDemoHttpJson(
                status_code=429,
                headers={
                    "X-Bapi-Limit": "100",
                    "X-Bapi-Limit-Status": "0",
                    "X-Bapi-Limit-Reset-Timestamp": "1015",
                },
                payload={"retCode": 0},
            )
        if calls == 2:
            return BybitDemoHttpJson(
                status_code=200,
                headers={"X-Bapi-Limit-Status": "0"},
                payload={"retCode": 10006, "retMsg": "Too many visits"},
            )
        return _fee_success()

    client = BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=_transport,
        clock_ms=lambda: 1_000,
        rest_policy=_policy(),
        sleep_fn=sleeps.append,
    )

    client.get_fee_rate(symbol="BTCUSDT")

    assert calls == 3
    assert sleeps == [0.015, 0.02]


def test_safe_get_clock_skew_is_not_retried() -> None:
    calls = 0

    def _transport(_method, _url, _headers, _body):
        nonlocal calls
        calls += 1
        return BybitDemoHttpJson(
            status_code=200,
            headers={},
            payload={"retCode": 10002, "retMsg": "request time exceeds window"},
        )

    client = BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=_transport,
        clock_ms=lambda: 1_000,
        rest_policy=_policy(),
        sleep_fn=lambda _delay: None,
    )

    with pytest.raises(BybitRestClockSkewError) as captured:
        client.get_fee_rate(symbol="BTCUSDT")

    assert calls == 1
    assert captured.value.retryable_read is False
    assert captured.value.ambiguous_mutation is False


def test_safe_get_exhaustion_preserves_typed_rate_limit_error() -> None:
    calls = 0

    def _transport(_method, _url, _headers, _body):
        nonlocal calls
        calls += 1
        return BybitDemoHttpJson(
            status_code=200,
            headers={"X-Bapi-Limit": "100", "X-Bapi-Limit-Status": "0"},
            payload={"retCode": 10006, "retMsg": "Too many visits"},
        )

    client = BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=_transport,
        clock_ms=lambda: 1_000,
        rest_policy=_policy(attempts=2),
        sleep_fn=lambda _delay: None,
    )

    with pytest.raises(BybitRestRateLimitError) as captured:
        client.get_fee_rate(symbol="BTCUSDT")

    assert calls == 2
    assert captured.value.ret_code == 10006
    assert captured.value.rate_limit is not None
    assert captured.value.rate_limit.limit == 100
    assert captured.value.rate_limit.remaining == 0


def test_money_moving_post_transport_timeout_is_never_retried() -> None:
    calls = 0

    def _transport(_method, _url, _headers, _body):
        nonlocal calls
        calls += 1
        raise TimeoutError("simulated lost acknowledgement")

    client = BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=_transport,
        clock_ms=lambda: 1_000,
        rest_policy=_policy(),
        sleep_fn=lambda _delay: pytest.fail("POST must not sleep/retry"),
    )

    with pytest.raises(BybitRestTransportError) as captured:
        client.place_market_order(
            BybitDemoOrderRequest(
                symbol="BTCUSDT",
                side="Buy",
                quantity=Decimal("0.001"),
                order_link_id="ASTRA-DEMO-REST-AMBIGUOUS",
            )
        )

    assert calls == 1
    assert captured.value.ambiguous_mutation is True
    assert captured.value.retryable_read is False
    assert "demo-key" not in str(captured.value)
    assert "demo-secret" not in str(captured.value)


def test_money_moving_post_server_timeout_is_single_shot_and_ambiguous() -> None:
    calls = 0

    def _transport(_method, _url, _headers, _body):
        nonlocal calls
        calls += 1
        return BybitDemoHttpJson(
            status_code=200,
            headers={},
            payload={"retCode": 10000, "retMsg": "Server Timeout"},
        )

    client = BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=_transport,
        clock_ms=lambda: 1_000,
        rest_policy=_policy(),
        sleep_fn=lambda _delay: pytest.fail("POST must not sleep/retry"),
    )

    with pytest.raises(BybitRestRequestError) as captured:
        client.place_market_order(
            BybitDemoOrderRequest(
                symbol="BTCUSDT",
                side="Sell",
                quantity=Decimal("0.001"),
                order_link_id="ASTRA-DEMO-REST-SERVER",
            )
        )

    assert calls == 1
    assert captured.value.ret_code == 10000
    assert captured.value.ambiguous_mutation is True


class _AccountingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, *, path, query_string, headers):
        del path, query_string, headers
        self.calls += 1
        if self.calls == 1:
            return BybitDemoAccountingHttpJson(
                status_code=429,
                headers={"X-Bapi-Limit-Status": "0"},
                payload={"retCode": 0},
            )
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "accountType": "UNIFIED",
                        "totalEquity": "1000",
                        "totalWalletBalance": "990",
                        "totalMarginBalance": "995",
                        "totalAvailableBalance": "900",
                        "totalPerpUPL": "5",
                        "totalInitialMargin": "50",
                        "totalMaintenanceMargin": "10",
                    }
                ],
                "nextPageCursor": "",
            },
        }


def test_accounting_read_uses_same_bounded_retry_policy() -> None:
    transport = _AccountingTransport()
    sleeps: list[float] = []
    client = BybitDemoAccountingClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=transport,
        clock_ms=lambda: 1_000,
        rest_policy=_policy(),
        sleep_fn=sleeps.append,
    )

    wallet = client.get_wallet_balance()

    assert transport.calls == 2
    assert sleeps == [0.01]
    assert wallet.total_equity_usd == Decimal("1000")


def test_rate_limit_header_parser_is_case_insensitive_and_non_secret() -> None:
    snapshot = parse_rate_limit_headers(
        {
            "x-bapi-limit": "120",
            "X-BAPI-LIMIT-STATUS": "119",
            "X-Bapi-Limit-Reset-Timestamp": "123456789",
            "X-BAPI-API-KEY": "must-not-be-copied",
        }
    )

    assert snapshot.limit == 120
    assert snapshot.remaining == 119
    assert snapshot.reset_timestamp_ms == 123456789
    assert "must-not-be-copied" not in repr(snapshot)
