from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperAdapterV100,
    AlpacaPaperConfigurationError,
    AlpacaPaperCredentialsV100,
    AlpacaPaperEndpointsV100,
    AlpacaPaperPolicyV100,
    AlpacaPaperProtocolError,
    AlpacaPaperRateLimitExceeded,
    AlpacaTradeUpdateStreamV100,
    HttpResponseV100,
    StaleStreamGeneration,
    TokenBucketV100,
    TradeStreamStateV100,
)
from app.runtime.paper_broker_contract_v99 import BrokerMutationError, OrderSide

UTC = timezone.utc
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class ScriptedTransport:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(status: int, payload=None) -> HttpResponseV100:
    body = b"" if payload is None else json.dumps(payload).encode()
    return HttpResponseV100(status, {}, body)


def order_payload(*, status="new", filled="0", updated="2026-08-03T12:00:00Z"):
    return {
        "id": "broker-1",
        "client_order_id": "client-1",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "limit_price": "100",
        "status": status,
        "filled_qty": filled,
        "updated_at": updated,
    }


def credentials() -> AlpacaPaperCredentialsV100:
    return AlpacaPaperCredentialsV100(key_id="paper-key", secret_key="paper-secret")


def test_credentials_are_opaque_and_environment_bound(monkeypatch) -> None:
    monkeypatch.setenv("ASTRA_ALPACA_PAPER_KEY_ID", "paper-key")
    monkeypatch.setenv("ASTRA_ALPACA_PAPER_SECRET_KEY", "paper-secret")
    value = AlpacaPaperCredentialsV100.from_environment()
    assert "paper-secret" not in repr(value)
    assert len(value.fingerprint) == 16
    with pytest.raises(AlpacaPaperConfigurationError):
        AlpacaPaperCredentialsV100.from_environment({})


def test_live_endpoints_are_rejected() -> None:
    with pytest.raises(AlpacaPaperConfigurationError):
        AlpacaPaperEndpointsV100(rest_base_url="https://api.alpaca.markets").validate()


def test_token_bucket_is_bounded_and_refills() -> None:
    clock = FakeClock()
    bucket = TokenBucketV100(capacity=1, refill_per_second=1, clock=clock)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    clock.value = 1
    assert bucket.try_acquire() is True


def test_reads_retry_but_mutations_do_not() -> None:
    transport = ScriptedTransport([TimeoutError("network"), response(200, {
        "id": "paper-account", "status": "ACTIVE", "currency": "USD",
        "buying_power": "1000", "trading_blocked": False,
    })])
    adapter = AlpacaPaperAdapterV100(
        credentials=credentials(), transport=transport,
        policy=AlpacaPaperPolicyV100(initial_backoff_seconds=0, maximum_backoff_seconds=0),
        sleeper=lambda _: None,
    )
    assert adapter.get_account().account_id == "paper-account"
    assert len(transport.calls) == 2

    mutation_transport = ScriptedTransport([TimeoutError("ambiguous")])
    writer = AlpacaPaperAdapterV100(
        credentials=credentials(), transport=mutation_transport,
        paper_order_writes_enabled=True,
    )
    with pytest.raises(BrokerMutationError) as caught:
        writer.submit_limit_order(
            client_order_id="client-1", instrument="AAPL", side=OrderSide.BUY,
            quantity=Decimal("1"), limit_price=Decimal("100"),
        )
    assert caught.value.ambiguous is True
    assert len(mutation_transport.calls) == 1


def test_writes_disabled_and_mutation_rate_limited() -> None:
    disabled = AlpacaPaperAdapterV100(credentials=credentials(), transport=ScriptedTransport([]))
    with pytest.raises(BrokerMutationError):
        disabled.submit_limit_order(
            client_order_id="x", instrument="AAPL", side=OrderSide.BUY,
            quantity=Decimal("1"), limit_price=Decimal("100"),
        )
    clock = FakeClock()
    writer = AlpacaPaperAdapterV100(
        credentials=credentials(),
        transport=ScriptedTransport([response(200, order_payload())]),
        policy=AlpacaPaperPolicyV100(mutation_capacity=1, mutation_refill_per_second=0.1),
        paper_order_writes_enabled=True,
        clock=clock,
    )
    writer.submit_limit_order(
        client_order_id="client-1", instrument="AAPL", side=OrderSide.BUY,
        quantity=Decimal("1"), limit_price=Decimal("100"),
    )
    with pytest.raises(AlpacaPaperRateLimitExceeded):
        writer.submit_limit_order(
            client_order_id="client-2", instrument="AAPL", side=OrderSide.BUY,
            quantity=Decimal("1"), limit_price=Decimal("100"),
        )


def auth_frame(status="authorized"):
    return json.dumps({"stream": "authorization", "data": {"status": status}})


def listening_frame():
    return json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}})


def update_frame(*, filled="0", updated="2026-08-03T12:00:00Z"):
    return json.dumps({"stream": "trade_updates", "data": {
        "event": "new", "order": order_payload(filled=filled, updated=updated),
    }})


def listening_stream() -> AlpacaTradeUpdateStreamV100:
    stream = AlpacaTradeUpdateStreamV100(generation=7, credentials=credentials())
    stream.authentication_frame()
    stream.ingest(auth_frame(), received_at=NOW, expected_generation=7)
    stream.ingest(listening_frame(), received_at=NOW, expected_generation=7)
    return stream


def test_authenticated_stream_binary_updates_and_duplicates() -> None:
    stream = listening_stream()
    update = stream.ingest(update_frame().encode(), received_at=NOW, expected_generation=7)
    assert update is not None and update.order.instrument == "AAPL"
    assert stream.ingest(update_frame(), received_at=NOW, expected_generation=7) is None
    evidence = stream.evidence(captured_at=NOW + timedelta(seconds=1))
    assert evidence["ready"] is True
    assert evidence["duplicate_updates"] == 1
    assert evidence["live_trading_allowed"] is False


def test_stream_generation_and_order_regressions_fail_closed() -> None:
    stream = listening_stream()
    with pytest.raises(StaleStreamGeneration):
        stream.ingest(update_frame(), received_at=NOW, expected_generation=6)
    stream.ingest(update_frame(filled="0.5"), received_at=NOW, expected_generation=7)
    with pytest.raises(AlpacaPaperProtocolError):
        stream.ingest(update_frame(filled="0.1", updated="2026-08-03T12:00:01Z"), received_at=NOW, expected_generation=7)
    assert stream.state is TradeStreamStateV100.QUARANTINED


def test_stream_requires_authorization_and_freshness() -> None:
    stream = AlpacaTradeUpdateStreamV100(generation=1, credentials=credentials(), maximum_silence=timedelta(seconds=1))
    with pytest.raises(AlpacaPaperProtocolError):
        stream.ingest(listening_frame(), received_at=NOW, expected_generation=1)
    stream = listening_stream()
    assert stream.evidence(captured_at=NOW + timedelta(minutes=1))["ready"] is False


@pytest.mark.parametrize(
    "factory",
    [
        lambda: AlpacaPaperCredentialsV100(key_id="", secret_key="x"),
        lambda: AlpacaPaperCredentialsV100(key_id="bad key", secret_key="x"),
        lambda: AlpacaPaperEndpointsV100(stream_url="wss://api.alpaca.markets/stream").validate(),
        lambda: AlpacaPaperPolicyV100(maximum_read_attempts=0).validate(),
        lambda: AlpacaPaperPolicyV100(initial_backoff_seconds=-1).validate(),
        lambda: AlpacaPaperPolicyV100(initial_backoff_seconds=1, maximum_backoff_seconds=0).validate(),
        lambda: AlpacaPaperPolicyV100(read_capacity=0).validate(),
        lambda: AlpacaPaperPolicyV100(read_refill_per_second=0).validate(),
        lambda: AlpacaPaperPolicyV100(timeout_seconds=0).validate(),
        lambda: TokenBucketV100(capacity=0, refill_per_second=1),
    ],
)
def test_invalid_configuration_is_rejected(factory) -> None:
    with pytest.raises((AlpacaPaperConfigurationError, ValueError)):
        factory()


def test_order_reads_replace_cancel_and_repr() -> None:
    transport = ScriptedTransport(
        [
            response(200, [order_payload()]),
            response(404, {"code": 404, "message": "not found"}),
            response(200, order_payload(status="replaced")),
            response(204),
            response(200, order_payload(status="canceled")),
        ]
    )
    adapter = AlpacaPaperAdapterV100(
        credentials=credentials(), transport=transport, paper_order_writes_enabled=True
    )
    assert "paper-secret" not in repr(adapter)
    assert len(adapter.list_open_orders()) == 1
    assert adapter.get_order_by_client_order_id("missing") is None
    assert adapter.replace_limit_order(
        broker_order_id="broker-1", limit_price=Decimal("101")
    ).status.value == "REPLACED"
    assert adapter.cancel_order(broker_order_id="broker-1").status.value == "CANCELLED"


def test_response_and_parser_failures_are_fail_closed() -> None:
    adapter = AlpacaPaperAdapterV100(
        credentials=credentials(), transport=ScriptedTransport([response(200, [])])
    )
    with pytest.raises(AlpacaPaperProtocolError):
        adapter.get_account()

    malformed = AlpacaPaperAdapterV100(
        credentials=credentials(),
        transport=ScriptedTransport([HttpResponseV100(200, {}, b"not-json")]),
    )
    with pytest.raises(AlpacaPaperProtocolError):
        malformed.get_account()

    bad_orders = AlpacaPaperAdapterV100(
        credentials=credentials(), transport=ScriptedTransport([response(200, {})])
    )
    with pytest.raises(AlpacaPaperProtocolError):
        bad_orders.list_open_orders()

    with pytest.raises(AlpacaPaperProtocolError):
        AlpacaPaperAdapterV100._decimal("NaN", "x")
    with pytest.raises(AlpacaPaperProtocolError):
        AlpacaPaperAdapterV100._parse_order([])
    with pytest.raises(AlpacaPaperProtocolError):
        AlpacaPaperAdapterV100._parse_order({**order_payload(), "status": "mystery"})
    with pytest.raises(AlpacaPaperProtocolError):
        AlpacaPaperAdapterV100._parse_order({**order_payload(), "side": "hold"})
    with pytest.raises(AlpacaPaperProtocolError):
        AlpacaPaperAdapterV100._parse_order({**order_payload(), "updated_at": "bad"})


def test_read_rate_limit_and_deterministic_4xx() -> None:
    clock = FakeClock()
    adapter = AlpacaPaperAdapterV100(
        credentials=credentials(),
        transport=ScriptedTransport([response(400, {"code": 400, "message": "bad"})]),
        policy=AlpacaPaperPolicyV100(read_capacity=1, read_refill_per_second=0.1),
        clock=clock,
    )
    with pytest.raises(BrokerMutationError) as caught:
        adapter.get_account()
    assert caught.value.ambiguous is False
    with pytest.raises(AlpacaPaperRateLimitExceeded):
        adapter.get_account()


def test_stream_protocol_failures_and_evidence() -> None:
    stream = AlpacaTradeUpdateStreamV100(generation=1, credentials=credentials())
    evidence = stream.evidence(captured_at=NOW)
    assert evidence["ready"] is False
    assert "NO_STREAM_MESSAGES" in evidence["reasons"]
    stream.authentication_frame()
    with pytest.raises(AlpacaPaperProtocolError):
        stream.ingest(auth_frame("unauthorized"), received_at=NOW, expected_generation=1)

    stream = AlpacaTradeUpdateStreamV100(generation=1, credentials=credentials())
    stream.authentication_frame()
    stream.ingest(auth_frame(), received_at=NOW, expected_generation=1)
    with pytest.raises(AlpacaPaperProtocolError):
        stream.ingest(
            json.dumps({"stream": "listening", "data": {"streams": []}}),
            received_at=NOW,
            expected_generation=1,
        )

    stream = listening_stream()
    with pytest.raises(AlpacaPaperProtocolError):
        stream.ingest(
            json.dumps({"stream": "trade_updates", "data": {"event": "mystery", "order": order_payload()}}),
            received_at=NOW,
            expected_generation=7,
        )

    stream = listening_stream()
    assert stream.ingest(json.dumps({"action": "error"}), received_at=NOW, expected_generation=7) is None
    assert stream.state is TradeStreamStateV100.DEGRADED

    stream = listening_stream()
    with pytest.raises(AlpacaPaperProtocolError):
        stream.ingest(json.dumps({"stream": "other"}), received_at=NOW, expected_generation=7)
    with pytest.raises(AlpacaPaperProtocolError):
        AlpacaTradeUpdateStreamV100._decode(b"\xff")
    with pytest.raises(AlpacaPaperProtocolError):
        AlpacaTradeUpdateStreamV100._decode("[]")
    with pytest.raises(AlpacaPaperProtocolError):
        AlpacaTradeUpdateStreamV100._mapping([], "x")


def test_stream_constructor_and_time_regression() -> None:
    with pytest.raises(ValueError):
        AlpacaTradeUpdateStreamV100(generation=0, credentials=credentials())
    with pytest.raises(ValueError):
        AlpacaTradeUpdateStreamV100(
            generation=1, credentials=credentials(), maximum_silence=timedelta(0)
        )
    assert "trade_updates" in AlpacaTradeUpdateStreamV100.listen_frame()

    stream = listening_stream()
    stream.ingest(update_frame(updated="2026-08-03T12:00:01Z"), received_at=NOW, expected_generation=7)
    with pytest.raises(AlpacaPaperProtocolError):
        stream.ingest(update_frame(updated="2026-08-03T12:00:00Z"), received_at=NOW, expected_generation=7)
    assert stream.state is TradeStreamStateV100.QUARANTINED
