from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.marketdata.bybit_public import (
    BYBIT_PUBLIC_LINEAR_STREAM,
    BybitPublicLinearFrameProcessor,
    BybitPublicLinearSession,
    BybitPublicLinearSubscription,
    BybitPublicMarketDataPolicy,
    BybitPublicProtocolError,
    WebsocketsBybitPublicConnector,
)
from app.marketdata.operational import SQLiteOperationalMarketDataStore

BASE = datetime(2026, 9, 16, 10, 5, tzinfo=UTC)
START_MS = int(datetime(2026, 9, 16, 10, 0, tzinfo=UTC).timestamp() * 1000)
END_MS = START_MS + 5 * 60 * 1000 - 1
TOPIC = "kline.5.BTCUSDT"


def subscription() -> BybitPublicLinearSubscription:
    return BybitPublicLinearSubscription(
        symbol="BTCUSDT",
        interval="5",
        strategy_id="bybit-demo-momentum-v1",
    )


def kline_frame(
    *,
    confirm: bool,
    server_ms: int | None = None,
    end_ms: int = END_MS,
    close: str = "100.5",
    matched_ms: int | None = None,
) -> str:
    effective_server_ms = int(BASE.timestamp() * 1000) if server_ms is None else server_ms
    effective_matched_ms = END_MS - 500 if matched_ms is None else matched_ms
    return json.dumps(
        {
            "topic": TOPIC,
            "type": "snapshot",
            "ts": effective_server_ms,
            "data": [
                {
                    "start": START_MS,
                    "end": end_ms,
                    "interval": "5",
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": close,
                    "volume": "12.5",
                    "turnover": "1256.25",
                    "confirm": confirm,
                    "timestamp": effective_matched_ms,
                }
            ],
        },
        separators=(",", ":"),
    )


def ack_frame() -> str:
    return json.dumps(
        {
            "success": True,
            "ret_msg": "",
            "conn_id": "connection-1",
            "req_id": "astra-f22b-subscribe",
            "op": "subscribe",
        }
    )


def pong_frame() -> str:
    return json.dumps(
        {
            "success": True,
            "ret_msg": "pong",
            "conn_id": "connection-1",
            "req_id": "astra-f22b-ping",
            "op": "ping",
        }
    )


@dataclass
class FakeClock:
    value: datetime = BASE

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeConnection:
    def __init__(self, *, clock: FakeClock, events: list[object]) -> None:
        self.clock = clock
        self.events = list(events)
        self.sent: list[str] = []
        self.closed = False

    def send(self, message: str | bytes) -> None:
        self.sent.append(message.decode() if isinstance(message, bytes) else message)

    def recv(self, timeout: float | None = None) -> str | bytes:
        del timeout
        if not self.events:
            raise AssertionError("fake stream exhausted")
        event = self.events.pop(0)
        if isinstance(event, TimeoutEvent):
            self.clock.advance(event.seconds)
            raise TimeoutError("fake timeout")
        self.clock.advance(1)
        if not isinstance(event, (str, bytes)):
            raise AssertionError("invalid fake stream event")
        return event

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class TimeoutEvent:
    seconds: float


class FakeConnector:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.urls: list[str] = []

    def __call__(self, url: str, *, timeout_seconds: float) -> FakeConnection:
        assert timeout_seconds > 0
        self.urls.append(url)
        return self.connection


def store(path: Path) -> SQLiteOperationalMarketDataStore:
    return SQLiteOperationalMarketDataStore(path)


def processor(path: Path) -> tuple[BybitPublicLinearFrameProcessor, SQLiteOperationalMarketDataStore]:
    value = store(path)
    return (
        BybitPublicLinearFrameProcessor(subscription=subscription(), store=value),
        value,
    )


def test_public_connector_rejects_non_allowlisted_endpoint_before_dependency_load() -> None:
    connector = WebsocketsBybitPublicConnector()
    with pytest.raises(BybitPublicProtocolError, match="non-allowlisted"):
        connector("wss://stream-demo.bybit.com", timeout_seconds=5)


def test_operational_subscription_rejects_calendar_intervals() -> None:
    value = BybitPublicLinearSubscription(
        symbol="BTCUSDT",
        interval="D",
        strategy_id="strategy",
    )
    with pytest.raises(ValueError, match="unsupported"):
        value.validate()


def test_kline_before_subscription_acknowledgement_fails_closed(tmp_path) -> None:
    value, persistence = processor(tmp_path / "marketdata.sqlite")
    with pytest.raises(BybitPublicProtocolError, match="before subscription"):
        value.process(
            kline_frame(confirm=True),
            received_at=BASE + timedelta(seconds=1),
            subscription_acknowledged=False,
        )
    assert persistence.pending_decisions() == ()


def test_in_progress_kline_updates_liveness_without_decision(tmp_path) -> None:
    clock = FakeClock()
    connection = FakeConnection(clock=clock, events=[ack_frame(), kline_frame(confirm=False)])
    persistence = store(tmp_path / "marketdata.sqlite")
    session = BybitPublicLinearSession(
        subscription=subscription(),
        store=persistence,
        connector=FakeConnector(connection),
        clock=clock,
    )

    evidence = session.run(maximum_receive_attempts=2)

    assert evidence.healthy
    assert evidence.subscription_acknowledged
    assert evidence.in_progress_frames == 1
    assert evidence.finalized_bars == 0
    assert persistence.pending_decisions() == ()
    assert connection.closed
    assert json.loads(connection.sent[0])["op"] == "subscribe"


def test_final_kline_creates_one_durable_ticket_and_overlap_is_idempotent(tmp_path) -> None:
    clock = FakeClock()
    first = kline_frame(confirm=True)
    second = kline_frame(
        confirm=True,
        server_ms=int((BASE + timedelta(seconds=1)).timestamp() * 1000),
    )
    connection = FakeConnection(clock=clock, events=[ack_frame(), first, second])
    persistence = store(tmp_path / "marketdata.sqlite")
    session = BybitPublicLinearSession(
        subscription=subscription(),
        store=persistence,
        connector=FakeConnector(connection),
        clock=clock,
    )

    evidence = session.run(maximum_receive_attempts=3)

    tickets = persistence.pending_decisions(strategy_id=subscription().strategy_id)
    assert evidence.healthy
    assert evidence.finalized_bars == 2
    assert len(tickets) == 1
    bars = persistence.recent_bars(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_close_time=BASE,
        limit=10,
    )
    assert len(bars) == 1
    assert bars[0].source_event_id == f"{TOPIC}:{START_MS}:{END_MS}"


def test_changed_final_economics_uses_existing_conflict_quarantine(tmp_path) -> None:
    value, persistence = processor(tmp_path / "marketdata.sqlite")
    value.process(
        kline_frame(confirm=True),
        received_at=BASE + timedelta(seconds=1),
        subscription_acknowledged=True,
    )
    with pytest.raises(RuntimeError, match="OPERATIONAL_BAR_CONFLICT"):
        value.process(
            kline_frame(confirm=True, close="100.75"),
            received_at=BASE + timedelta(seconds=2),
            subscription_acknowledged=True,
        )

    assert persistence.conflict_count() == 1
    assert persistence.pending_decisions() == ()


def test_fractional_or_wrong_bybit_boundary_is_rejected_before_persistence(tmp_path) -> None:
    value, persistence = processor(tmp_path / "marketdata.sqlite")
    with pytest.raises(BybitPublicProtocolError, match="boundaries"):
        value.process(
            kline_frame(confirm=True, end_ms=END_MS + 1),
            received_at=BASE + timedelta(seconds=1),
            subscription_acknowledged=True,
        )
    assert persistence.pending_decisions() == ()


def test_future_server_clock_is_rejected_before_persistence(tmp_path) -> None:
    value, persistence = processor(tmp_path / "marketdata.sqlite")
    future_ms = int((BASE + timedelta(seconds=10)).timestamp() * 1000)
    with pytest.raises(BybitPublicProtocolError, match="CLOCK_IN_FUTURE"):
        value.process(
            kline_frame(confirm=True, server_ms=future_ms),
            received_at=BASE,
            subscription_acknowledged=True,
        )
    assert persistence.pending_decisions() == ()


def test_stale_server_data_is_rejected_before_persistence(tmp_path) -> None:
    persistence = store(tmp_path / "marketdata.sqlite")
    policy = BybitPublicMarketDataPolicy(maximum_server_age_seconds=10)
    value = BybitPublicLinearFrameProcessor(
        subscription=subscription(),
        store=persistence,
        policy=policy,
    )
    stale_ms = int((BASE - timedelta(seconds=11)).timestamp() * 1000)
    with pytest.raises(BybitPublicProtocolError, match="DATA_STALE"):
        value.process(
            kline_frame(confirm=True, server_ms=stale_ms),
            received_at=BASE,
            subscription_acknowledged=True,
        )
    assert persistence.pending_decisions() == ()


def test_heartbeat_ping_and_pong_are_application_control_only(tmp_path) -> None:
    clock = FakeClock()
    connection = FakeConnection(
        clock=clock,
        events=[ack_frame(), TimeoutEvent(21), pong_frame()],
    )
    persistence = store(tmp_path / "marketdata.sqlite")
    session = BybitPublicLinearSession(
        subscription=subscription(),
        store=persistence,
        connector=FakeConnector(connection),
        clock=clock,
    )

    evidence = session.run(maximum_receive_attempts=3)

    sent = [json.loads(message) for message in connection.sent]
    assert evidence.healthy
    assert evidence.last_pong_at is not None
    assert [message["op"] for message in sent] == ["subscribe", "ping"]
    assert persistence.pending_decisions() == ()


def test_stream_silence_is_reported_fail_closed(tmp_path) -> None:
    clock = FakeClock()
    connection = FakeConnection(clock=clock, events=[ack_frame(), TimeoutEvent(91)])
    persistence = store(tmp_path / "marketdata.sqlite")
    session = BybitPublicLinearSession(
        subscription=subscription(),
        store=persistence,
        connector=FakeConnector(connection),
        clock=clock,
    )

    evidence = session.run(maximum_receive_attempts=2)

    assert not evidence.healthy
    assert evidence.reasons == ("STREAM_SILENT",)
    assert persistence.pending_decisions() == ()


def test_failed_subscription_ack_does_not_create_market_truth(tmp_path) -> None:
    value, persistence = processor(tmp_path / "marketdata.sqlite")
    failure = json.dumps({"success": False, "ret_msg": "topic invalid", "op": "subscribe"})
    with pytest.raises(BybitPublicProtocolError, match="not accepted"):
        value.process(
            failure,
            received_at=BASE,
            subscription_acknowledged=False,
        )
    assert persistence.pending_decisions() == ()


def test_public_session_has_no_authentication_or_broker_mutation_surface(tmp_path) -> None:
    clock = FakeClock()
    connection = FakeConnection(clock=clock, events=[ack_frame()])
    session = BybitPublicLinearSession(
        subscription=subscription(),
        store=store(tmp_path / "marketdata.sqlite"),
        connector=FakeConnector(connection),
        clock=clock,
    )
    session.run(maximum_receive_attempts=1)

    payloads = [json.loads(message) for message in connection.sent]
    assert all(payload.get("op") not in {"auth", "order.create", "order.amend", "order.cancel"} for payload in payloads)
    assert not hasattr(session, "submit_order")
    assert not hasattr(session, "cancel_order")
    assert BYBIT_PUBLIC_LINEAR_STREAM == "wss://stream.bybit.com/v5/public/linear"
