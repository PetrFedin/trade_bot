from decimal import Decimal

import pytest

from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperAdapterV100,
    AlpacaPaperCredentialsV100,
    AlpacaPaperPolicyV100,
    AlpacaPaperProtocolError,
    AlpacaPaperRateLimitExceeded,
    HttpResponseV100,
)


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def request(self, method, url, *, headers, body, timeout_seconds):
        self.urls.append(url)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def credentials():
    return AlpacaPaperCredentialsV100(key_id="paper-key", secret_key="paper-secret")


def account_response():
    return HttpResponseV100(
        200,
        {},
        b'{"id":"paper","status":"ACTIVE","currency":"USD","buying_power":"1000","trading_blocked":false}',
    )


def test_client_order_id_is_percent_encoded() -> None:
    transport = ScriptedTransport([HttpResponseV100(404, {}, b'{"code":404,"message":"missing"}')])
    adapter = AlpacaPaperAdapterV100(credentials=credentials(), transport=transport)
    assert adapter.get_order_by_client_order_id("client id/?&=") is None
    assert transport.urls == [
        "https://paper-api.alpaca.markets/v2/orders:by_client_order_id?client_order_id=client%20id%2F%3F%26%3D"
    ]


def test_each_read_attempt_consumes_a_rate_limit_token() -> None:
    transport = ScriptedTransport([TimeoutError("first"), account_response()])
    adapter = AlpacaPaperAdapterV100(
        credentials=credentials(),
        transport=transport,
        policy=AlpacaPaperPolicyV100(
            maximum_read_attempts=2,
            initial_backoff_seconds=0,
            maximum_backoff_seconds=0,
            read_capacity=1,
            read_refill_per_second=0.000001,
        ),
        sleeper=lambda _: None,
    )
    with pytest.raises(AlpacaPaperRateLimitExceeded):
        adapter.get_account()
    assert len(transport.urls) == 1


def test_oversized_broker_response_fails_closed() -> None:
    transport = ScriptedTransport([HttpResponseV100(200, {}, b"{}" * 10)])
    adapter = AlpacaPaperAdapterV100(
        credentials=credentials(),
        transport=transport,
        policy=AlpacaPaperPolicyV100(maximum_response_bytes=4),
    )
    with pytest.raises(AlpacaPaperProtocolError, match="exceeds configured size limit"):
        adapter.get_account()


def test_response_size_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="maximum_response_bytes"):
        AlpacaPaperPolicyV100(maximum_response_bytes=0).validate()


def test_broker_order_id_is_percent_encoded_for_mutations() -> None:
    payload = (
        b'{"id":"broker/1","client_order_id":"client-1","symbol":"AAPL",'
        b'"side":"buy","qty":"1","limit_price":"100","status":"replaced",'
        b'"filled_qty":"0","updated_at":"2026-08-03T12:00:00Z"}'
    )
    transport = ScriptedTransport([HttpResponseV100(200, {}, payload)])
    adapter = AlpacaPaperAdapterV100(
        credentials=credentials(), transport=transport, paper_order_writes_enabled=True
    )
    adapter.replace_limit_order(broker_order_id="broker/1", limit_price=Decimal("100"))
    assert transport.urls[0].endswith("/v2/orders/broker%2F1")
