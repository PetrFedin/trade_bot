from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import ssl

import pytest

from app.runtime.alpaca_external_probe_v101 import (
    AlpacaPaperGateway,
    ConfigurationError,
    Credentials,
    ExternalProbeError,
    GatewayPolicy,
    HttpResponse,
    PAPER_REST,
    PAPER_STREAM,
    ProtocolError,
    ReadOnlyExternalProbe,
    UrllibTransport,
    WebsocketsConnector,
)
from app.runtime.sandbox_qualification_v101 import AmbiguousMutation, OrderStatus

UTC = timezone.utc
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append((method, url, headers, body, timeout_seconds))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def response(payload, status=200):
    return HttpResponse(status, {}, json.dumps(payload).encode())


def account_payload():
    return {"id": "acct-1", "status": "ACTIVE", "currency": "USD", "buying_power": "1000", "trading_blocked": False}


def order_payload(status="new", price="10", filled="0"):
    return {
        "client_order_id": "astra-q-1",
        "id": "broker-1",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "1",
        "limit_price": price,
        "status": status,
        "filled_qty": filled,
        "updated_at": "2026-08-03T12:00:00Z",
    }


def credentials():
    return Credentials("paper-key", "paper-secret")


def test_credentials_are_redacted_and_loaded():
    creds = Credentials.from_environment({Credentials.KEY_ENV: "key", Credentials.SECRET_ENV: "secret"})
    assert "secret" not in repr(creds)
    assert len(creds.fingerprint) == 16
    assert creds.headers()["APCA-API-KEY-ID"] == "key"
    with pytest.raises(ConfigurationError):
        Credentials.from_environment({})
    with pytest.raises(ValueError):
        Credentials("", "x")


def test_account_and_orders_parsing():
    transport = Transport([response(account_payload()), response([order_payload()])])
    gateway = AlpacaPaperGateway(credentials=credentials(), transport=transport)
    assert gateway.get_account().account_id == "acct-1"
    orders = gateway.list_open_orders()
    assert orders[0].status is OrderStatus.ACKNOWLEDGED
    assert transport.calls[0][1].startswith(PAPER_REST)


def test_reads_retry_but_mutation_does_not_retry():
    transport = Transport([TimeoutError(), response(account_payload())])
    gateway = AlpacaPaperGateway(
        credentials=credentials(), transport=transport,
        policy=GatewayPolicy(read_attempts=2, retry_backoff_seconds=0), sleeper=lambda _: None,
    )
    assert gateway.get_account().account_id == "acct-1"
    assert len(transport.calls) == 2

    mutation_transport = Transport([TimeoutError(), response(order_payload())])
    writer = AlpacaPaperGateway(credentials=credentials(), transport=mutation_transport, writes_enabled=True)
    with pytest.raises(AmbiguousMutation):
        writer.submit_limit_order(
            client_order_id="astra-q-1", symbol="AAPL", side=__import__("app.runtime.sandbox_qualification_v101", fromlist=["Side"]).Side.BUY,
            quantity=Decimal("1"), limit_price=Decimal("10"),
        )
    assert len(mutation_transport.calls) == 1


def test_mutations_require_explicit_enablement():
    gateway = AlpacaPaperGateway(credentials=credentials(), transport=Transport([]))
    with pytest.raises(ConfigurationError):
        gateway.cancel_order(broker_order_id="broker-1")


def test_submit_replace_cancel_and_lookup():
    transport = Transport([
        response(order_payload()),
        response(order_payload(status="replaced", price="11")),
        HttpResponse(204, {}, b""),
        response(order_payload(status="canceled", price="11")),
        response(order_payload(status="canceled", price="11")),
    ])
    gateway = AlpacaPaperGateway(credentials=credentials(), transport=transport, writes_enabled=True)
    from app.runtime.sandbox_qualification_v101 import Side
    assert gateway.submit_limit_order(client_order_id="astra-q-1", symbol="AAPL", side=Side.BUY, quantity=Decimal("1"), limit_price=Decimal("10")).status is OrderStatus.ACKNOWLEDGED
    assert gateway.replace_limit_order(broker_order_id="broker-1", limit_price=Decimal("11")).status is OrderStatus.REPLACED
    assert gateway.cancel_order(broker_order_id="broker-1").status is OrderStatus.CANCELLED
    assert gateway.get_order_by_client_order_id("astra-q-1").status is OrderStatus.CANCELLED


def test_lookup_404_returns_none():
    gateway = AlpacaPaperGateway(credentials=credentials(), transport=Transport([response({"message": "not found"}, 404)]))
    assert gateway.get_order_by_client_order_id("missing") is None


def test_protocol_errors():
    gateway = AlpacaPaperGateway(credentials=credentials(), transport=Transport([HttpResponse(200, {}, b"bad")]))
    with pytest.raises(ProtocolError):
        gateway.get_account()
    with pytest.raises(ProtocolError):
        AlpacaPaperGateway._parse_order({"status": "unknown"})


class Connection:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def recv(self, timeout=None):
        value = self.frames.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class Connector:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def __call__(self, url, *, timeout_seconds):
        self.calls.append((url, timeout_seconds))
        return self.connection


def test_read_only_external_probe():
    transport = Transport([response(account_payload()), response([])])
    gateway = AlpacaPaperGateway(credentials=credentials(), transport=transport)
    connection = Connection([
        json.dumps({"stream": "authorization", "data": {"status": "authorized"}}),
        json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}}).encode(),
    ])
    probe = ReadOnlyExternalProbe(credentials=credentials(), gateway=gateway, connector=Connector(connection), generation=7)
    account, orders, evidence = probe.run(now=NOW)
    assert account.account_id == "acct-1"
    assert orders == ()
    assert evidence.authenticated and evidence.listening
    assert connection.closed
    assert "paper-secret" in connection.sent[0]


def test_read_only_probe_failure_and_handshake_incomplete():
    gateway = AlpacaPaperGateway(credentials=credentials(), transport=Transport([response(account_payload()), response([])]))
    failing = Connection([TimeoutError()])
    with pytest.raises(ExternalProbeError):
        ReadOnlyExternalProbe(credentials=credentials(), gateway=gateway, connector=Connector(failing), generation=7).run(now=NOW)

    gateway2 = AlpacaPaperGateway(credentials=credentials(), transport=Transport([response(account_payload()), response([])]))
    connection = Connection([json.dumps({"data": {"status": "denied"}}), json.dumps({"data": {"streams": []}})])
    _, _, evidence = ReadOnlyExternalProbe(credentials=credentials(), gateway=gateway2, connector=Connector(connection), generation=7).run(now=NOW)
    assert not evidence.authenticated
    assert evidence.reasons == ("STREAM_HANDSHAKE_INCOMPLETE",)


def test_probe_requires_write_disabled_gateway():
    gateway = AlpacaPaperGateway(credentials=credentials(), transport=Transport([]), writes_enabled=True)
    with pytest.raises(ConfigurationError):
        ReadOnlyExternalProbe(credentials=credentials(), gateway=gateway, connector=Connector(Connection([])), generation=7)


def test_urllib_transport_tls_policy():
    context = ssl.create_default_context()
    transport = UrllibTransport(context)
    assert transport.context.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(ProtocolError):
        transport.request("GET", "https://example.com/x", headers={}, body=None, timeout_seconds=1)


def test_websocket_connector_rejects_nonpaper():
    with pytest.raises(ProtocolError):
        WebsocketsConnector()("wss://example.com", timeout_seconds=1)
