from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.alpaca_live import (
    AlpacaBarUpdate,
    AlpacaBarUpdateKind,
    AlpacaLiveMarketDataSession,
    AlpacaMarketDataPolicy,
    AlpacaMarketDataProtocolError,
    AlpacaMarketDataSinkError,
    AlpacaMarketDataStreamState,
    AlpacaMarketDataTransportError,
    AlpacaStockMarketDataStream,
    StaleMarketDataGeneration,
)
from app.marketdata.operational import SQLiteOperationalMarketDataStore

NOW = datetime(2026, 9, 16, 10, 1, 1, tzinfo=UTC)


class FakeCredentials:
    def websocket_auth_document(self) -> Mapping[str, str]:
        return {"action": "auth", "key": "key", "secret": "secret"}


class CollectingSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.updates: list[AlpacaBarUpdate] = []
        self.fail = fail

    def accept(self, update: AlpacaBarUpdate) -> None:
        if self.fail:
            raise RuntimeError("sink unavailable")
        self.updates.append(update)


class FakeSocket:
    def __init__(self, frames: list[str], *, hang_after_frames: bool = False) -> None:
        self.frames = list(frames)
        self.sent: list[bytes | str] = []
        self.hang_after_frames = hang_after_frames

    async def send(self, message: bytes | str) -> None:
        self.sent.append(message)

    async def recv(self) -> bytes | str:
        if self.frames:
            return self.frames.pop(0)
        if self.hang_after_frames:
            await asyncio.sleep(60)
        raise ConnectionError("fake socket closed")


class FakeSocketContext(AbstractAsyncContextManager[FakeSocket]):
    def __init__(self, socket: FakeSocket, *, fail_enter: bool = False) -> None:
        self.socket = socket
        self.fail_enter = fail_enter

    async def __aenter__(self) -> FakeSocket:
        if self.fail_enter:
            raise OSError("connect failed")
        return self.socket

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class FakeSocketFactory:
    def __init__(self, socket: FakeSocket, *, fail_enter: bool = False) -> None:
        self.socket = socket
        self.fail_enter = fail_enter
        self.urls: list[str] = []

    def __call__(self, url: str) -> FakeSocketContext:
        self.urls.append(url)
        return FakeSocketContext(self.socket, fail_enter=self.fail_enter)


def frame(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def connected() -> str:
    return frame([{"T": "success", "msg": "connected"}])


def authenticated() -> str:
    return frame([{"T": "success", "msg": "authenticated"}])


def subscribed(symbols: list[str] | None = None) -> str:
    values = ["AAPL"] if symbols is None else symbols
    return frame(
        [
            {
                "T": "subscription",
                "trades": [],
                "quotes": [],
                "bars": values,
                "updatedBars": values,
                "dailyBars": [],
                "statuses": [],
                "lulds": [],
                "corrections": [],
                "cancelErrors": [],
            }
        ]
    )


def bar_message(
    *,
    message_type: str = "b",
    timestamp: str = "2026-09-16T10:00:00Z",
    close: str = "101",
) -> str:
    return frame(
        [
            {
                "T": message_type,
                "S": "AAPL",
                "o": "100",
                "h": "102",
                "l": "99",
                "c": close,
                "v": "1000",
                "n": 20,
                "vw": "100.5",
                "t": timestamp,
            }
        ]
    )


def stream(*, policy: AlpacaMarketDataPolicy | None = None) -> AlpacaStockMarketDataStream:
    return AlpacaStockMarketDataStream(
        generation=7,
        credentials=FakeCredentials(),
        symbols=("AAPL",),
        policy=policy,
    )


def handshake(value: AlpacaStockMarketDataStream) -> None:
    assert value.ingest(connected(), received_at=NOW, expected_generation=7) == ()
    assert value.state is AlpacaMarketDataStreamState.CONNECTED
    auth = json.loads(value.authentication_frame())
    assert auth == {"action": "auth", "key": "key", "secret": "secret"}
    value.ingest(authenticated(), received_at=NOW, expected_generation=7)
    assert value.state is AlpacaMarketDataStreamState.AUTHENTICATED
    subscription = json.loads(value.subscription_frame())
    assert subscription == {
        "action": "subscribe",
        "bars": ["AAPL"],
        "updatedBars": ["AAPL"],
    }
    value.ingest(subscribed(), received_at=NOW, expected_generation=7)
    assert value.state is AlpacaMarketDataStreamState.SUBSCRIBED


def test_handshake_is_explicit_read_only_and_no_bar_means_not_ready() -> None:
    value = stream()
    handshake(value)

    evidence = value.evidence(captured_at=NOW)
    assert not evidence.ready
    assert evidence.reasons == ("NO_MARKET_DATA_BARS",)
    assert not evidence.external_order_routing_allowed
    assert not evidence.live_trading_allowed
    assert not hasattr(value, "submit_order")
    assert not hasattr(value, "cancel_order")
    assert not hasattr(value, "replace_order")


def test_subscription_ack_must_exactly_match_dedicated_bar_channels() -> None:
    value = stream()
    value.ingest(connected(), received_at=NOW, expected_generation=7)
    value.authentication_frame()
    value.ingest(authenticated(), received_at=NOW, expected_generation=7)
    value.subscription_frame()

    with pytest.raises(AlpacaMarketDataProtocolError, match="does not match request"):
        value.ingest(subscribed(["MSFT"]), received_at=NOW, expected_generation=7)

    assert value.state is AlpacaMarketDataStreamState.QUARANTINED
    assert "SUBSCRIPTION_SET_MISMATCH" in value.evidence(captured_at=NOW).reasons


def test_base_bar_is_provisional_source_fact_not_final_operational_bar() -> None:
    value = stream()
    handshake(value)

    updates = value.ingest(bar_message(), received_at=NOW, expected_generation=7)
    assert len(updates) == 1
    update = updates[0]
    assert update.provider == "ALPACA"
    assert update.venue == "IEX"
    assert update.symbol == "AAPL"
    assert update.interval_seconds == 60
    assert update.open_time == datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
    assert update.close_time == datetime(2026, 9, 16, 10, 1, tzinfo=UTC)
    assert update.close == Decimal("101")
    assert update.kind is AlpacaBarUpdateKind.BASE
    assert not hasattr(update, "is_final")
    assert not hasattr(update, "strategy_bar")

    evidence = value.evidence(captured_at=NOW)
    assert evidence.ready
    assert evidence.accepted_base_bars == 1
    assert evidence.maximum_delivery_lag_seconds == 1.0


def test_updated_bar_is_same_bar_identity_but_separate_provisional_revision() -> None:
    value = stream()
    handshake(value)

    base = value.ingest(bar_message(), received_at=NOW, expected_generation=7)[0]
    updated = value.ingest(
        bar_message(message_type="u", close="101.5"),
        received_at=NOW + timedelta(seconds=30),
        expected_generation=7,
    )[0]

    assert base.kind is AlpacaBarUpdateKind.BASE
    assert updated.kind is AlpacaBarUpdateKind.UPDATED
    assert base.bar_identity == updated.bar_identity
    assert updated.close == Decimal("101.5")
    assert value.evidence(captured_at=NOW + timedelta(seconds=30)).accepted_updated_bars == 1


def test_duplicate_stream_message_is_idempotent() -> None:
    value = stream()
    handshake(value)
    raw = bar_message()

    first = value.ingest(raw, received_at=NOW, expected_generation=7)
    second = value.ingest(raw, received_at=NOW + timedelta(seconds=1), expected_generation=7)

    assert len(first) == 1
    assert second == ()
    assert value.evidence(captured_at=NOW + timedelta(seconds=1)).duplicate_messages == 1


def test_invalid_frame_quarantines_stream() -> None:
    value = stream()
    handshake(value)

    with pytest.raises(AlpacaMarketDataProtocolError, match="invalid market-data JSON"):
        value.ingest("{", received_at=NOW, expected_generation=7)

    assert value.state is AlpacaMarketDataStreamState.QUARANTINED
    assert "INVALID_MARKET_DATA_FRAME" in value.evidence(captured_at=NOW).reasons


def test_stale_generation_cannot_enter_operational_stream() -> None:
    value = stream()
    with pytest.raises(StaleMarketDataGeneration):
        value.ingest(connected(), received_at=NOW, expected_generation=6)
    assert value.state is AlpacaMarketDataStreamState.CONNECTING


def test_future_bar_quarantines_stream() -> None:
    value = stream()
    handshake(value)

    with pytest.raises(AlpacaMarketDataProtocolError, match="ahead of receive clock"):
        value.ingest(
            bar_message(timestamp="2026-09-16T10:01:00Z"),
            received_at=NOW,
            expected_generation=7,
        )

    assert value.state is AlpacaMarketDataStreamState.QUARANTINED
    assert "BAR_FROM_FUTURE" in value.evidence(captured_at=NOW).reasons


def test_updated_bar_requires_observed_base_in_same_generation() -> None:
    value = stream()
    handshake(value)

    with pytest.raises(AlpacaMarketDataProtocolError, match="without observed base"):
        value.ingest(bar_message(message_type="u"), received_at=NOW, expected_generation=7)

    assert value.state is AlpacaMarketDataStreamState.QUARANTINED


def test_out_of_order_base_bar_quarantines_stream() -> None:
    value = stream()
    handshake(value)
    value.ingest(
        bar_message(timestamp="2026-09-16T10:00:00Z"),
        received_at=NOW,
        expected_generation=7,
    )

    with pytest.raises(AlpacaMarketDataProtocolError, match="regressed"):
        value.ingest(
            bar_message(timestamp="2026-09-16T09:59:00Z", close="100"),
            received_at=NOW,
            expected_generation=7,
        )

    assert value.state is AlpacaMarketDataStreamState.QUARANTINED


def test_receive_clock_regression_quarantines_stream() -> None:
    value = stream()
    handshake(value)
    value.ingest(bar_message(), received_at=NOW, expected_generation=7)

    with pytest.raises(AlpacaMarketDataProtocolError, match="receive clock regressed"):
        value.ingest(
            frame([{"T": "subscription", "bars": ["AAPL"], "updatedBars": ["AAPL"]}]),
            received_at=NOW - timedelta(seconds=1),
            expected_generation=7,
        )

    assert "RECEIVE_CLOCK_REGRESSION" in value.evidence(captured_at=NOW).reasons


def test_silence_evidence_degrades_readiness_without_enabling_live() -> None:
    value = stream()
    handshake(value)
    value.ingest(bar_message(), received_at=NOW, expected_generation=7)

    evidence = value.evidence(captured_at=NOW + timedelta(minutes=2))
    assert not evidence.ready
    assert "MARKET_DATA_STREAM_STALE" in evidence.reasons
    assert "MARKET_DATA_BAR_STALE" in evidence.reasons
    assert not evidence.live_trading_allowed


def test_session_delivers_provisional_update_without_creating_decision_ticket(tmp_path) -> None:
    socket = FakeSocket([connected(), authenticated(), subscribed(), bar_message()])
    factory = FakeSocketFactory(socket)
    sink = CollectingSink()
    value = stream()
    decision_store = SQLiteOperationalMarketDataStore(tmp_path / "marketdata.sqlite")
    session = AlpacaLiveMarketDataSession(
        stream=value,
        sink=sink,
        socket_factory=factory,
        clock=lambda: NOW,
    )

    result = asyncio.run(session.run_once(maximum_market_frames=1))

    assert result.frames_processed == 1
    assert result.updates_delivered == 1
    assert len(sink.updates) == 1
    assert sink.updates[0].kind is AlpacaBarUpdateKind.BASE
    assert decision_store.pending_decisions() == ()
    assert result.evidence.ready
    assert value.state is AlpacaMarketDataStreamState.CLOSED
    assert factory.urls == ["wss://stream.data.alpaca.markets/v2/iex"]
    sent = [json.loads(message) for message in socket.sent]
    assert sent == [
        {"action": "auth", "key": "key", "secret": "secret"},
        {"action": "subscribe", "bars": ["AAPL"], "updatedBars": ["AAPL"]},
    ]


def test_session_detects_stream_silence_after_successful_handshake() -> None:
    policy = AlpacaMarketDataPolicy(maximum_stream_silence=timedelta(milliseconds=5))
    socket = FakeSocket([connected(), authenticated(), subscribed()], hang_after_frames=True)
    value = stream(policy=policy)
    session = AlpacaLiveMarketDataSession(
        stream=value,
        sink=CollectingSink(),
        socket_factory=FakeSocketFactory(socket),
        clock=lambda: NOW,
    )

    with pytest.raises(AlpacaMarketDataTransportError, match="exceeded silence budget"):
        asyncio.run(session.run_once(maximum_market_frames=1))

    assert value.state is AlpacaMarketDataStreamState.DEGRADED
    assert "MARKET_DATA_STREAM_SILENCE" in value.evidence(captured_at=NOW).reasons


def test_handshake_timeout_degrades_transport() -> None:
    policy = AlpacaMarketDataPolicy(handshake_timeout=timedelta(milliseconds=5))
    socket = FakeSocket([], hang_after_frames=True)
    value = stream(policy=policy)
    session = AlpacaLiveMarketDataSession(
        stream=value,
        sink=CollectingSink(),
        socket_factory=FakeSocketFactory(socket),
        clock=lambda: NOW,
    )

    with pytest.raises(AlpacaMarketDataTransportError, match="handshake timed out"):
        asyncio.run(session.run_once(maximum_market_frames=1))

    assert value.state is AlpacaMarketDataStreamState.DEGRADED
    assert "MARKET_DATA_HANDSHAKE_TIMEOUT" in value.evidence(captured_at=NOW).reasons


def test_connect_failure_is_classified_as_transport_failure() -> None:
    value = stream()
    session = AlpacaLiveMarketDataSession(
        stream=value,
        sink=CollectingSink(),
        socket_factory=FakeSocketFactory(FakeSocket([]), fail_enter=True),
        clock=lambda: NOW,
    )

    with pytest.raises(AlpacaMarketDataTransportError, match="connection failed"):
        asyncio.run(session.run_once(maximum_market_frames=1))

    assert value.state is AlpacaMarketDataStreamState.DEGRADED
    assert "MARKET_DATA_CONNECT_FAILURE" in value.evidence(captured_at=NOW).reasons


def test_sink_failure_degrades_stream_without_finalizing_source_fact() -> None:
    socket = FakeSocket([connected(), authenticated(), subscribed(), bar_message()])
    value = stream()
    session = AlpacaLiveMarketDataSession(
        stream=value,
        sink=CollectingSink(fail=True),
        socket_factory=FakeSocketFactory(socket),
        clock=lambda: NOW,
    )

    with pytest.raises(AlpacaMarketDataSinkError, match="sink rejected update"):
        asyncio.run(session.run_once(maximum_market_frames=1))

    assert value.state is AlpacaMarketDataStreamState.DEGRADED
    assert "MARKET_DATA_SINK_FAILURE" in value.evidence(captured_at=NOW).reasons
