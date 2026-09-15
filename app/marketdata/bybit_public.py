from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.marketdata.operational import (
    OperationalBar,
    OperationalDecisionTicket,
    OperationalMarketDataStore,
)

BYBIT_PUBLIC_LINEAR_STREAM = "wss://stream.bybit.com/v5/public/linear"
_PROVIDER = "BYBIT"
_VENUE = "BYBIT_LINEAR"
_SUPPORTED_MINUTE_INTERVALS = frozenset(
    {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720"}
)
_SUBSCRIBE_REQUEST_ID = "astra-f22b-subscribe"
_PING_REQUEST_ID = "astra-f22b-ping"


class BybitPublicMarketDataError(RuntimeError):
    pass


class BybitPublicProtocolError(BybitPublicMarketDataError):
    pass


class BybitPublicDependencyUnavailable(BybitPublicMarketDataError):
    pass


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class BybitPublicLinearSubscription:
    symbol: str
    interval: str
    strategy_id: str

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be non-empty normalized uppercase")
        if self.interval not in _SUPPORTED_MINUTE_INTERVALS:
            raise ValueError("unsupported Bybit operational kline interval")
        if not self.strategy_id.strip() or self.strategy_id != self.strategy_id.strip():
            raise ValueError("strategy_id must be non-empty and trimmed")

    @property
    def interval_seconds(self) -> int:
        self.validate()
        return int(self.interval) * 60

    @property
    def topic(self) -> str:
        self.validate()
        return f"kline.{self.interval}.{self.symbol}"


@dataclass(frozen=True)
class BybitPublicMarketDataPolicy:
    receive_timeout_seconds: float = 5.0
    heartbeat_interval_seconds: float = 20.0
    maximum_stream_silence_seconds: float = 90.0
    maximum_server_age_seconds: float = 90.0
    maximum_server_future_skew_seconds: float = 2.0

    def validate(self) -> None:
        if self.receive_timeout_seconds <= 0:
            raise ValueError("receive_timeout_seconds must be positive")
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        if self.maximum_stream_silence_seconds < self.heartbeat_interval_seconds:
            raise ValueError(
                "maximum_stream_silence_seconds must cover at least one heartbeat interval"
            )
        if self.maximum_server_age_seconds <= 0:
            raise ValueError("maximum_server_age_seconds must be positive")
        if self.maximum_server_future_skew_seconds < 0:
            raise ValueError("maximum_server_future_skew_seconds must be non-negative")


@dataclass(frozen=True)
class BybitPublicStreamEvidence:
    endpoint: str
    topic: str
    opened_at: datetime
    captured_at: datetime
    subscription_acknowledged: bool
    last_frame_at: datetime | None
    last_server_at: datetime | None
    last_pong_at: datetime | None
    receive_attempts: int
    finalized_bars: int
    in_progress_frames: int
    reasons: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not self.reasons

    def validate(self) -> None:
        if self.endpoint != BYBIT_PUBLIC_LINEAR_STREAM:
            raise ValueError("unexpected Bybit public endpoint")
        if not self.topic.startswith("kline."):
            raise ValueError("unexpected Bybit public topic")
        opened = _aware(self.opened_at, "opened_at")
        captured = _aware(self.captured_at, "captured_at")
        if captured < opened:
            raise ValueError("captured_at cannot precede opened_at")
        for name, value in (
            ("last_frame_at", self.last_frame_at),
            ("last_server_at", self.last_server_at),
            ("last_pong_at", self.last_pong_at),
        ):
            if value is not None:
                moment = _aware(value, name)
                if moment < opened:
                    raise ValueError(f"{name} cannot precede opened_at")
        counters = (self.receive_attempts, self.finalized_bars, self.in_progress_frames)
        if any(value < 0 for value in counters):
            raise ValueError("stream evidence counters must be non-negative")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("stream evidence reasons must be unique")


@dataclass(frozen=True)
class BybitPublicFrameResult:
    ticket: OperationalDecisionTicket | None = None
    final_bar: OperationalBar | None = None
    in_progress: bool = False
    subscription_acknowledged: bool = False
    pong: bool = False
    server_at: datetime | None = None


class WebSocketConnection(Protocol):
    def send(self, message: str | bytes) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self) -> None: ...


class WebSocketConnector(Protocol):
    def __call__(self, url: str, *, timeout_seconds: float) -> WebSocketConnection: ...


class WebsocketsBybitPublicConnector:
    """Credential-free connector restricted to one public market-data endpoint."""

    def __call__(self, url: str, *, timeout_seconds: float) -> WebSocketConnection:
        if url != BYBIT_PUBLIC_LINEAR_STREAM:
            raise BybitPublicProtocolError(
                "Bybit public connector rejected non-allowlisted endpoint"
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise BybitPublicDependencyUnavailable(
                "install the marketdata extra for Bybit public WebSocket support"
            ) from exc
        return connect(
            url,
            open_timeout=timeout_seconds,
            close_timeout=timeout_seconds,
            ping_interval=None,
            max_size=1_048_576,
            max_queue=16,
        )


class BybitPublicLinearFrameProcessor:
    """Strict parser/adopter for one public-linear kline subscription."""

    def __init__(
        self,
        *,
        subscription: BybitPublicLinearSubscription,
        store: OperationalMarketDataStore,
        policy: BybitPublicMarketDataPolicy | None = None,
    ) -> None:
        resolved_policy = BybitPublicMarketDataPolicy() if policy is None else policy
        subscription.validate()
        resolved_policy.validate()
        self.subscription = subscription
        self.store = store
        self.policy = resolved_policy

    def process(
        self,
        frame: str | bytes,
        *,
        received_at: datetime,
        subscription_acknowledged: bool,
    ) -> BybitPublicFrameResult:
        received = _aware(received_at, "received_at")
        payload = _decode_frame(frame)
        op = payload.get("op")
        if op == "subscribe":
            return self._subscription_ack(payload)
        if op in {"ping", "pong"}:
            return self._pong(payload, received_at=received)

        topic = payload.get("topic")
        if topic is None:
            raise BybitPublicProtocolError("unrecognized Bybit public control frame")
        if not subscription_acknowledged:
            raise BybitPublicProtocolError(
                "kline received before subscription acknowledgement"
            )
        if topic != self.subscription.topic:
            raise BybitPublicProtocolError("unexpected Bybit public topic")
        if payload.get("type") != "snapshot":
            raise BybitPublicProtocolError("Bybit kline frame must be snapshot type")

        server_at = _milliseconds_timestamp(payload.get("ts"), "ts")
        self._validate_server_clock(server_at=server_at, received_at=received)
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise BybitPublicProtocolError(
                "Bybit kline frame must contain exactly one data object"
            )
        row = rows[0]
        if row.get("interval") != self.subscription.interval:
            raise BybitPublicProtocolError(
                "Bybit kline interval disagrees with subscription"
            )
        confirm = row.get("confirm")
        if not isinstance(confirm, bool):
            raise BybitPublicProtocolError("Bybit kline confirm must be boolean")
        if not confirm:
            self._validate_kline_boundaries(row)
            return BybitPublicFrameResult(in_progress=True, server_at=server_at)

        bar = self._final_bar(row, received_at=received)
        ticket = self.store.record_finalized_for_strategy(
            bar,
            strategy_id=self.subscription.strategy_id,
            recorded_at=received,
        )
        return BybitPublicFrameResult(
            ticket=ticket,
            final_bar=bar,
            server_at=server_at,
        )

    def _subscription_ack(self, payload: Mapping[str, object]) -> BybitPublicFrameResult:
        if payload.get("success") is not True:
            raise BybitPublicProtocolError("Bybit public subscription was not accepted")
        request_id = payload.get("req_id")
        if request_id not in (None, "", _SUBSCRIBE_REQUEST_ID):
            raise BybitPublicProtocolError(
                "Bybit subscription acknowledgement request id mismatch"
            )
        ret_msg = payload.get("ret_msg")
        if ret_msg not in (None, "", "subscribe"):
            raise BybitPublicProtocolError(
                "unexpected Bybit subscription acknowledgement"
            )
        return BybitPublicFrameResult(subscription_acknowledged=True)

    @staticmethod
    def _pong(
        payload: Mapping[str, object],
        *,
        received_at: datetime,
    ) -> BybitPublicFrameResult:
        op = payload.get("op")
        if op == "ping":
            if payload.get("success") is not True or payload.get("ret_msg") != "pong":
                raise BybitPublicProtocolError(
                    "invalid Bybit public pong acknowledgement"
                )
        elif op != "pong":
            raise BybitPublicProtocolError("invalid Bybit pong frame")
        return BybitPublicFrameResult(pong=True, server_at=received_at)

    def _validate_server_clock(
        self,
        *,
        server_at: datetime,
        received_at: datetime,
    ) -> None:
        if server_at - received_at > timedelta(
            seconds=self.policy.maximum_server_future_skew_seconds
        ):
            raise BybitPublicProtocolError("BYBIT_SERVER_CLOCK_IN_FUTURE")
        if received_at - server_at > timedelta(
            seconds=self.policy.maximum_server_age_seconds
        ):
            raise BybitPublicProtocolError("BYBIT_SERVER_DATA_STALE")

    def _validate_kline_boundaries(
        self,
        row: Mapping[str, object],
    ) -> tuple[int, int]:
        start_ms = _integer(row.get("start"), "start")
        end_ms = _integer(row.get("end"), "end")
        expected_end = start_ms + self.subscription.interval_seconds * 1000 - 1
        if end_ms != expected_end:
            raise BybitPublicProtocolError(
                "Bybit kline boundaries disagree with interval"
            )
        return start_ms, end_ms

    def _final_bar(
        self,
        row: Mapping[str, object],
        *,
        received_at: datetime,
    ) -> OperationalBar:
        start_ms, end_ms = self._validate_kline_boundaries(row)
        source_timestamp = _milliseconds_timestamp(row.get("timestamp"), "timestamp")
        open_time = _from_milliseconds(start_ms)
        close_time = open_time + timedelta(seconds=self.subscription.interval_seconds)
        if source_timestamp < open_time or source_timestamp >= close_time:
            raise BybitPublicProtocolError(
                "Bybit matched-order timestamp lies outside candle"
            )
        source_event_id = f"{self.subscription.topic}:{start_ms}:{end_ms}"
        bar = OperationalBar(
            provider=_PROVIDER,
            venue=_VENUE,
            symbol=self.subscription.symbol,
            interval_seconds=self.subscription.interval_seconds,
            open_time=open_time,
            close_time=close_time,
            source_timestamp=source_timestamp,
            received_at=received_at,
            source_event_id=source_event_id,
            is_final=True,
            open=_decimal(row, "open"),
            high=_decimal(row, "high"),
            low=_decimal(row, "low"),
            close=_decimal(row, "close"),
            volume=_non_negative_decimal(row, "volume"),
            revision=0,
        )
        try:
            bar.validate()
        except ValueError as exc:
            raise BybitPublicProtocolError("invalid finalized Bybit kline") from exc
        return bar


class BybitPublicLinearSession:
    """Bounded read-only session; reconnect/backfill is deliberately outside F22B."""

    def __init__(
        self,
        *,
        subscription: BybitPublicLinearSubscription,
        store: OperationalMarketDataStore,
        connector: WebSocketConnector,
        policy: BybitPublicMarketDataPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        resolved_policy = BybitPublicMarketDataPolicy() if policy is None else policy
        subscription.validate()
        resolved_policy.validate()
        self.subscription = subscription
        self.policy = resolved_policy
        self.connector = connector
        self.clock = (lambda: datetime.now(UTC)) if clock is None else clock
        self.processor = BybitPublicLinearFrameProcessor(
            subscription=subscription,
            store=store,
            policy=resolved_policy,
        )

    def run(self, *, maximum_receive_attempts: int) -> BybitPublicStreamEvidence:
        if maximum_receive_attempts < 1:
            raise ValueError("maximum_receive_attempts must be positive")
        opened_at = _aware(self.clock(), "clock")
        last_frame_at: datetime | None = None
        last_server_at: datetime | None = None
        last_pong_at: datetime | None = None
        last_ping_at = opened_at
        ping_outstanding = False
        acknowledged = False
        finalized_bars = 0
        in_progress_frames = 0
        receive_attempts = 0
        reasons: list[str] = []

        connection = self.connector(
            BYBIT_PUBLIC_LINEAR_STREAM,
            timeout_seconds=self.policy.receive_timeout_seconds,
        )
        try:
            connection.send(public_linear_subscription_message(self.subscription))
            while receive_attempts < maximum_receive_attempts:
                before_receive = _aware(self.clock(), "clock")
                if (
                    not ping_outstanding
                    and before_receive - last_ping_at
                    >= timedelta(seconds=self.policy.heartbeat_interval_seconds)
                ):
                    _send_ping(connection)
                    last_ping_at = before_receive
                    ping_outstanding = True

                receive_attempts += 1
                try:
                    frame = connection.recv(
                        timeout=self.policy.receive_timeout_seconds
                    )
                except TimeoutError:
                    now = _aware(self.clock(), "clock")
                    if now - (last_frame_at or opened_at) > timedelta(
                        seconds=self.policy.maximum_stream_silence_seconds
                    ):
                        reasons.append("STREAM_SILENT")
                        break
                    if (
                        not ping_outstanding
                        and now - last_ping_at
                        >= timedelta(seconds=self.policy.heartbeat_interval_seconds)
                    ):
                        _send_ping(connection)
                        last_ping_at = now
                        ping_outstanding = True
                    continue
                except OSError as exc:
                    raise BybitPublicMarketDataError(
                        "Bybit public stream transport failed"
                    ) from exc

                received_at = _aware(self.clock(), "clock")
                last_frame_at = received_at
                result = self.processor.process(
                    frame,
                    received_at=received_at,
                    subscription_acknowledged=acknowledged,
                )
                if result.subscription_acknowledged:
                    acknowledged = True
                if result.pong:
                    last_pong_at = received_at
                    ping_outstanding = False
                if result.server_at is not None:
                    last_server_at = result.server_at
                if result.in_progress:
                    in_progress_frames += 1
                if result.final_bar is not None:
                    finalized_bars += 1
        finally:
            connection.close()

        captured_at = _aware(self.clock(), "clock")
        if not acknowledged:
            reasons.append("SUBSCRIPTION_NOT_ACKNOWLEDGED")
        if ping_outstanding:
            reasons.append("HEARTBEAT_UNACKNOWLEDGED")
        if (
            captured_at - (last_frame_at or opened_at)
            > timedelta(seconds=self.policy.maximum_stream_silence_seconds)
            and "STREAM_SILENT" not in reasons
        ):
            reasons.append("STREAM_SILENT")
        evidence = BybitPublicStreamEvidence(
            endpoint=BYBIT_PUBLIC_LINEAR_STREAM,
            topic=self.subscription.topic,
            opened_at=opened_at,
            captured_at=captured_at,
            subscription_acknowledged=acknowledged,
            last_frame_at=last_frame_at,
            last_server_at=last_server_at,
            last_pong_at=last_pong_at,
            receive_attempts=receive_attempts,
            finalized_bars=finalized_bars,
            in_progress_frames=in_progress_frames,
            reasons=tuple(dict.fromkeys(reasons)),
        )
        evidence.validate()
        return evidence


def _send_ping(connection: WebSocketConnection) -> None:
    connection.send(
        json.dumps(
            {"req_id": _PING_REQUEST_ID, "op": "ping"},
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _decode_frame(frame: str | bytes) -> Mapping[str, object]:
    try:
        text = frame.decode() if isinstance(frame, bytes) else frame
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BybitPublicProtocolError("invalid Bybit public JSON frame") from exc
    if not isinstance(payload, Mapping):
        raise BybitPublicProtocolError("Bybit public frame must be a JSON object")
    return payload


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BybitPublicProtocolError(
            f"Bybit kline {field} must be an integer"
        )
    return value


def _from_milliseconds(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _milliseconds_timestamp(value: object, field: str) -> datetime:
    return _from_milliseconds(_integer(value, field))


def _decimal(row: Mapping[str, object], field: str) -> Decimal:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise BybitPublicProtocolError(
            f"Bybit kline {field} is missing or invalid"
        )
    try:
        result = Decimal(str(value))
    except ArithmeticError as exc:
        raise BybitPublicProtocolError(
            f"Bybit kline {field} is not decimal"
        ) from exc
    if not result.is_finite() or result <= 0:
        raise BybitPublicProtocolError(
            f"Bybit kline {field} must be positive and finite"
        )
    return result


def _non_negative_decimal(row: Mapping[str, object], field: str) -> Decimal:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise BybitPublicProtocolError(
            f"Bybit kline {field} is missing or invalid"
        )
    try:
        result = Decimal(str(value))
    except ArithmeticError as exc:
        raise BybitPublicProtocolError(
            f"Bybit kline {field} is not decimal"
        ) from exc
    if not result.is_finite() or result < 0:
        raise BybitPublicProtocolError(
            f"Bybit kline {field} must be finite and non-negative"
        )
    return result


def public_linear_subscription_message(
    subscription: BybitPublicLinearSubscription,
) -> str:
    subscription.validate()
    return json.dumps(
        {
            "req_id": _SUBSCRIBE_REQUEST_ID,
            "op": "subscribe",
            "args": [subscription.topic],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
