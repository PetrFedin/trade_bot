from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlparse


class AlpacaMarketDataError(RuntimeError):
    pass


class AlpacaMarketDataProtocolError(AlpacaMarketDataError):
    pass


class AlpacaMarketDataTransportError(AlpacaMarketDataError):
    pass


class AlpacaMarketDataSinkError(AlpacaMarketDataError):
    pass


class StaleMarketDataGeneration(AlpacaMarketDataError):
    pass


class AlpacaStockFeed(StrEnum):
    IEX = "iex"
    SIP = "sip"
    DELAYED_SIP = "delayed_sip"
    TEST = "test"

    @property
    def venue(self) -> str:
        return {
            AlpacaStockFeed.IEX: "IEX",
            AlpacaStockFeed.SIP: "US-SIP",
            AlpacaStockFeed.DELAYED_SIP: "US-SIP-DELAYED",
            AlpacaStockFeed.TEST: "ALPACA-TEST",
        }[self]


class AlpacaMarketDataStreamState(StrEnum):
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    AUTHENTICATING = "AUTHENTICATING"
    AUTHENTICATED = "AUTHENTICATED"
    SUBSCRIBING = "SUBSCRIBING"
    SUBSCRIBED = "SUBSCRIBED"
    DEGRADED = "DEGRADED"
    QUARANTINED = "QUARANTINED"
    CLOSED = "CLOSED"


class AlpacaBarUpdateKind(StrEnum):
    BASE = "BASE"
    UPDATED = "UPDATED"


class MarketDataCredentials(Protocol):
    def websocket_auth_document(self) -> Mapping[str, str]: ...


class MarketDataSocket(Protocol):
    async def send(self, message: bytes | str) -> None: ...

    async def recv(self) -> bytes | str: ...


class MarketDataSocketFactory(Protocol):
    def __call__(self, url: str) -> AbstractAsyncContextManager[MarketDataSocket]: ...


class AlpacaBarUpdateSink(Protocol):
    def accept(self, update: AlpacaBarUpdate) -> None: ...


@dataclass(frozen=True)
class AlpacaMarketDataEndpoint:
    feed: AlpacaStockFeed = AlpacaStockFeed.IEX

    @property
    def stream_url(self) -> str:
        if self.feed is AlpacaStockFeed.TEST:
            return "wss://stream.data.alpaca.markets/v2/test"
        return f"wss://stream.data.alpaca.markets/v2/{self.feed.value}"

    @property
    def rest_base_url(self) -> str:
        return "https://data.alpaca.markets"

    def validate(self) -> None:
        stream = urlparse(self.stream_url)
        if stream.scheme != "wss" or stream.hostname != "stream.data.alpaca.markets":
            raise ValueError("market-data stream endpoint must be Alpaca data-plane WSS")
        if stream.username or stream.password or stream.query or stream.fragment:
            raise ValueError("market-data stream endpoint must be a clean URL")
        rest = urlparse(self.rest_base_url)
        if rest.scheme != "https" or rest.hostname != "data.alpaca.markets":
            raise ValueError("market-data REST endpoint must be Alpaca data plane")


@dataclass(frozen=True)
class AlpacaMarketDataPolicy:
    handshake_timeout: timedelta = timedelta(seconds=10)
    maximum_stream_silence: timedelta = timedelta(seconds=45)
    maximum_bar_silence: timedelta = timedelta(seconds=90)
    maximum_future_skew: timedelta = timedelta(seconds=2)
    base_identity_retention: timedelta = timedelta(minutes=10)
    maximum_dedup_messages: int = 10_000
    websocket_open_timeout_seconds: float = 10.0
    websocket_ping_interval_seconds: float = 20.0
    websocket_ping_timeout_seconds: float = 20.0
    websocket_maximum_message_bytes: int = 2_000_000

    def validate(self) -> None:
        if self.handshake_timeout <= timedelta(0):
            raise ValueError("handshake_timeout must be positive")
        if self.maximum_stream_silence <= timedelta(0):
            raise ValueError("maximum_stream_silence must be positive")
        if self.maximum_bar_silence <= timedelta(0):
            raise ValueError("maximum_bar_silence must be positive")
        if self.maximum_future_skew < timedelta(0):
            raise ValueError("maximum_future_skew cannot be negative")
        if self.base_identity_retention <= timedelta(0):
            raise ValueError("base_identity_retention must be positive")
        if self.maximum_dedup_messages < 1:
            raise ValueError("maximum_dedup_messages must be positive")
        if self.websocket_open_timeout_seconds <= 0:
            raise ValueError("websocket_open_timeout_seconds must be positive")
        if self.websocket_ping_interval_seconds <= 0:
            raise ValueError("websocket_ping_interval_seconds must be positive")
        if self.websocket_ping_timeout_seconds <= 0:
            raise ValueError("websocket_ping_timeout_seconds must be positive")
        if self.websocket_maximum_message_bytes < 1:
            raise ValueError("websocket_maximum_message_bytes must be positive")


@dataclass(frozen=True)
class AlpacaBarUpdate:
    """Provisional source fact. F22C decides when it is safe to finalize."""

    provider: str
    venue: str
    symbol: str
    interval_seconds: int
    open_time: datetime
    close_time: datetime
    received_at: datetime
    source_event_id: str
    kind: AlpacaBarUpdateKind
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def validate(self) -> None:
        if self.provider != "ALPACA":
            raise ValueError("provider must be ALPACA")
        if not self.venue or self.venue != self.venue.strip().upper():
            raise ValueError("venue must be normalized uppercase")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be normalized uppercase")
        if self.interval_seconds != 60:
            raise ValueError("F22B supports one-minute stock bars only")
        if not isinstance(self.kind, AlpacaBarUpdateKind):
            raise ValueError("kind must be an AlpacaBarUpdateKind")
        open_time = _aware(self.open_time, "open_time")
        close_time = _aware(self.close_time, "close_time")
        received_at = _aware(self.received_at, "received_at")
        if close_time - open_time != timedelta(seconds=self.interval_seconds):
            raise ValueError("bar boundaries disagree with interval_seconds")
        if received_at < open_time:
            raise ValueError("received_at cannot precede bar open")
        if not self.source_event_id.strip():
            raise ValueError("source_event_id is required")
        for name, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if (
            not isinstance(self.volume, Decimal)
            or not self.volume.is_finite()
            or self.volume < 0
        ):
            raise ValueError("volume must be a finite non-negative Decimal")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high is below bar prices")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low is above bar prices")

    @property
    def bar_identity(self) -> str:
        self.validate()
        raw = "|".join(
            (
                self.provider,
                self.venue,
                self.symbol,
                str(self.interval_seconds),
                _aware(self.open_time, "open_time").isoformat(),
                _aware(self.close_time, "close_time").isoformat(),
            )
        )
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class AlpacaMarketDataEvidence:
    generation: int
    state: AlpacaMarketDataStreamState
    captured_at: datetime
    last_message_at: datetime | None
    last_bar_received_at: datetime | None
    last_bar_close_time: datetime | None
    accepted_base_bars: int
    accepted_updated_bars: int
    duplicate_messages: int
    dedup_cache_size: int
    tracked_base_identities: int
    maximum_delivery_lag_seconds: float | None
    ready: bool
    reasons: tuple[str, ...]
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False


@dataclass(frozen=True)
class AlpacaMarketDataSessionResult:
    updates_delivered: int
    frames_processed: int
    evidence: AlpacaMarketDataEvidence


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AlpacaMarketDataProtocolError(f"invalid decimal field: {field}") from exc
    if not parsed.is_finite():
        raise AlpacaMarketDataProtocolError(f"non-finite decimal field: {field}")
    return parsed


def _timestamp(value: object, field: str) -> datetime:
    raw = str(value).strip()
    if not raw:
        raise AlpacaMarketDataProtocolError(f"missing timestamp field: {field}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlpacaMarketDataProtocolError(f"invalid timestamp field: {field}") from exc
    return _aware(parsed, field)


def _canonical_digest(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class AlpacaStockMarketDataStream:
    """Read-only Alpaca stock-bar protocol boundary with fail-closed evidence."""

    def __init__(
        self,
        *,
        generation: int,
        credentials: MarketDataCredentials,
        symbols: tuple[str, ...],
        endpoint: AlpacaMarketDataEndpoint | None = None,
        policy: AlpacaMarketDataPolicy | None = None,
    ) -> None:
        if generation < 1:
            raise ValueError("generation must be positive")
        normalized = tuple(sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()}))
        if not normalized or len(normalized) != len(symbols):
            raise ValueError("symbols must be unique non-empty normalized values")
        if any(symbol != symbol.strip().upper() for symbol in symbols):
            raise ValueError("symbols must already be normalized uppercase")
        self.generation = generation
        self.credentials = credentials
        self.symbols = normalized
        self.endpoint = AlpacaMarketDataEndpoint() if endpoint is None else endpoint
        self.policy = AlpacaMarketDataPolicy() if policy is None else policy
        self.endpoint.validate()
        self.policy.validate()
        self.state = AlpacaMarketDataStreamState.CONNECTING
        self.last_message_at: datetime | None = None
        self.last_bar_received_at: datetime | None = None
        self.last_bar_close_time: datetime | None = None
        self.accepted_base_bars = 0
        self.accepted_updated_bars = 0
        self.duplicate_messages = 0
        self._seen_messages: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._base_open_times: dict[tuple[str, datetime], datetime] = {}
        self._latest_base_close_by_symbol: dict[str, datetime] = {}
        self._reasons: set[str] = set()
        self._maximum_delivery_lag_seconds: float | None = None

    def authentication_frame(self) -> bytes:
        if self.state is not AlpacaMarketDataStreamState.CONNECTED:
            raise AlpacaMarketDataProtocolError("authentication requires CONNECTED state")
        document = dict(self.credentials.websocket_auth_document())
        if document.get("action") != "auth":
            raise AlpacaMarketDataProtocolError("credentials must produce Alpaca auth action")
        if not str(document.get("key", "")).strip() or not str(document.get("secret", "")).strip():
            raise AlpacaMarketDataProtocolError("market-data credentials are incomplete")
        self.state = AlpacaMarketDataStreamState.AUTHENTICATING
        return json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode()

    def subscription_frame(self) -> bytes:
        if self.state is not AlpacaMarketDataStreamState.AUTHENTICATED:
            raise AlpacaMarketDataProtocolError("subscription requires AUTHENTICATED state")
        self.state = AlpacaMarketDataStreamState.SUBSCRIBING
        return json.dumps(
            {
                "action": "subscribe",
                "bars": list(self.symbols),
                "updatedBars": list(self.symbols),
            },
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()

    def ingest(
        self,
        raw_frame: bytes | str,
        *,
        received_at: datetime,
        expected_generation: int,
    ) -> tuple[AlpacaBarUpdate, ...]:
        received = _aware(received_at, "received_at")
        if expected_generation != self.generation:
            raise StaleMarketDataGeneration("stale Alpaca market-data stream generation")
        if self.last_message_at is not None and received < self.last_message_at:
            self._quarantine("RECEIVE_CLOCK_REGRESSION")
            raise AlpacaMarketDataProtocolError("market-data receive clock regressed")
        try:
            messages = self._decode_frame(raw_frame)
        except AlpacaMarketDataProtocolError:
            self._quarantine("INVALID_MARKET_DATA_FRAME")
            raise
        produced: list[AlpacaBarUpdate] = []
        for message in messages:
            self.last_message_at = received
            digest = _canonical_digest(message)
            if not self._remember_message(digest):
                self.duplicate_messages += 1
                continue
            message_type = str(message.get("T", ""))
            if message_type == "success":
                self._ingest_success(message)
                continue
            if message_type == "subscription":
                self._ingest_subscription(message)
                continue
            if message_type == "error":
                self._quarantine("MARKET_DATA_STREAM_ERROR")
                raise AlpacaMarketDataProtocolError(
                    f"market-data stream error {message.get('code')}: {message.get('msg')}"
                )
            if message_type not in {"b", "u"}:
                self._quarantine("UNEXPECTED_MARKET_DATA_MESSAGE")
                raise AlpacaMarketDataProtocolError(
                    f"unexpected market-data message type: {message_type or '<missing>'}"
                )
            if self.state is not AlpacaMarketDataStreamState.SUBSCRIBED:
                self._quarantine("BAR_BEFORE_SUBSCRIPTION_ACK")
                raise AlpacaMarketDataProtocolError("bar received before subscription acknowledgement")
            produced.append(
                self._parse_bar_update(
                    message,
                    message_type=message_type,
                    digest=digest,
                    received_at=received,
                )
            )
        return tuple(produced)

    def evidence(self, *, captured_at: datetime) -> AlpacaMarketDataEvidence:
        captured = _aware(captured_at, "captured_at")
        reasons = set(self._reasons)
        if self.state is not AlpacaMarketDataStreamState.SUBSCRIBED:
            reasons.add("MARKET_DATA_STREAM_NOT_SUBSCRIBED")
        if self.last_message_at is None:
            reasons.add("NO_MARKET_DATA_MESSAGES")
        elif captured < self.last_message_at:
            reasons.add("MARKET_DATA_CLOCK_REGRESSION")
        elif captured - self.last_message_at > self.policy.maximum_stream_silence:
            reasons.add("MARKET_DATA_STREAM_STALE")
        if self.last_bar_received_at is None:
            reasons.add("NO_MARKET_DATA_BARS")
        elif captured < self.last_bar_received_at:
            reasons.add("MARKET_DATA_CLOCK_REGRESSION")
        elif captured - self.last_bar_received_at > self.policy.maximum_bar_silence:
            reasons.add("MARKET_DATA_BAR_STALE")
        return AlpacaMarketDataEvidence(
            generation=self.generation,
            state=self.state,
            captured_at=captured,
            last_message_at=self.last_message_at,
            last_bar_received_at=self.last_bar_received_at,
            last_bar_close_time=self.last_bar_close_time,
            accepted_base_bars=self.accepted_base_bars,
            accepted_updated_bars=self.accepted_updated_bars,
            duplicate_messages=self.duplicate_messages,
            dedup_cache_size=len(self._seen_messages),
            tracked_base_identities=len(self._base_open_times),
            maximum_delivery_lag_seconds=self._maximum_delivery_lag_seconds,
            ready=not reasons,
            reasons=tuple(sorted(reasons)),
        )

    def degrade(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("degrade reason is required")
        if self.state is not AlpacaMarketDataStreamState.QUARANTINED:
            self.state = AlpacaMarketDataStreamState.DEGRADED
        self._reasons.add(reason)

    def quarantine(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("quarantine reason is required")
        self._quarantine(reason)

    def close(self) -> None:
        if self.state not in {
            AlpacaMarketDataStreamState.DEGRADED,
            AlpacaMarketDataStreamState.QUARANTINED,
        }:
            self.state = AlpacaMarketDataStreamState.CLOSED

    def _remember_message(self, digest: str) -> bool:
        if digest in self._seen_messages:
            return False
        self._seen_messages.add(digest)
        self._seen_order.append(digest)
        while len(self._seen_order) > self.policy.maximum_dedup_messages:
            expired = self._seen_order.popleft()
            self._seen_messages.discard(expired)
        return True

    def _purge_base_identities(self, reference_close: datetime) -> None:
        cutoff = reference_close - self.policy.base_identity_retention
        expired = [
            identity
            for identity, close_time in self._base_open_times.items()
            if close_time < cutoff
        ]
        for identity in expired:
            del self._base_open_times[identity]

    def _ingest_success(self, message: Mapping[str, object]) -> None:
        status = str(message.get("msg", "")).lower()
        if status == "connected":
            if self.state is not AlpacaMarketDataStreamState.CONNECTING:
                self._quarantine("DUPLICATE_OR_LATE_CONNECTED_ACK")
                raise AlpacaMarketDataProtocolError("connected acknowledgement out of order")
            self.state = AlpacaMarketDataStreamState.CONNECTED
            return
        if status == "authenticated":
            if self.state is not AlpacaMarketDataStreamState.AUTHENTICATING:
                self._quarantine("AUTH_ACK_OUT_OF_ORDER")
                raise AlpacaMarketDataProtocolError("authentication acknowledgement out of order")
            self.state = AlpacaMarketDataStreamState.AUTHENTICATED
            return
        self._quarantine("UNKNOWN_SUCCESS_MESSAGE")
        raise AlpacaMarketDataProtocolError(f"unknown success message: {status}")

    def _ingest_subscription(self, message: Mapping[str, object]) -> None:
        if self.state is not AlpacaMarketDataStreamState.SUBSCRIBING:
            self._quarantine("SUBSCRIPTION_ACK_OUT_OF_ORDER")
            raise AlpacaMarketDataProtocolError("subscription acknowledgement out of order")
        expected = set(self.symbols)
        bars = self._string_set(message.get("bars"), "bars")
        updated = self._string_set(message.get("updatedBars"), "updatedBars")
        if bars != expected or updated != expected:
            self._quarantine("SUBSCRIPTION_SET_MISMATCH")
            raise AlpacaMarketDataProtocolError("subscription acknowledgement does not match request")
        for channel in (
            "trades",
            "quotes",
            "dailyBars",
            "statuses",
            "lulds",
            "corrections",
            "cancelErrors",
        ):
            if self._string_set(message.get(channel, []), channel):
                self._quarantine("UNEXPECTED_SUBSCRIPTION_CHANNEL")
                raise AlpacaMarketDataProtocolError(
                    f"dedicated bar connection acknowledged unexpected channel: {channel}"
                )
        self.state = AlpacaMarketDataStreamState.SUBSCRIBED

    def _parse_bar_update(
        self,
        message: Mapping[str, object],
        *,
        message_type: str,
        digest: str,
        received_at: datetime,
    ) -> AlpacaBarUpdate:
        symbol = str(message.get("S", "")).strip().upper()
        if symbol not in self.symbols:
            self._quarantine("UNSUBSCRIBED_SYMBOL")
            raise AlpacaMarketDataProtocolError(f"bar for unsubscribed symbol: {symbol}")
        open_time = _timestamp(message.get("t"), "t")
        close_time = open_time + timedelta(minutes=1)
        if close_time > received_at + self.policy.maximum_future_skew:
            self._quarantine("BAR_FROM_FUTURE")
            raise AlpacaMarketDataProtocolError("bar close time is ahead of receive clock")
        lag_seconds = max(0.0, (received_at - close_time).total_seconds())
        self._maximum_delivery_lag_seconds = (
            lag_seconds
            if self._maximum_delivery_lag_seconds is None
            else max(self._maximum_delivery_lag_seconds, lag_seconds)
        )
        identity = (symbol, open_time)
        prior_close = self._latest_base_close_by_symbol.get(symbol)
        if message_type == "b":
            if prior_close is not None and close_time < prior_close:
                self._quarantine("OUT_OF_ORDER_BASE_BAR")
                raise AlpacaMarketDataProtocolError("base bar time regressed")
            self._base_open_times[identity] = close_time
            self._latest_base_close_by_symbol[symbol] = (
                close_time if prior_close is None else max(prior_close, close_time)
            )
            self._purge_base_identities(close_time)
            kind = AlpacaBarUpdateKind.BASE
            self.accepted_base_bars += 1
        else:
            if identity not in self._base_open_times:
                self._quarantine("UPDATED_BAR_WITHOUT_BASE")
                raise AlpacaMarketDataProtocolError("updated bar arrived without observed base bar")
            kind = AlpacaBarUpdateKind.UPDATED
            self.accepted_updated_bars += 1
        update = AlpacaBarUpdate(
            provider="ALPACA",
            venue=self.endpoint.feed.venue,
            symbol=symbol,
            interval_seconds=60,
            open_time=open_time,
            close_time=close_time,
            received_at=received_at,
            source_event_id=digest,
            kind=kind,
            open=_decimal(message.get("o"), "o"),
            high=_decimal(message.get("h"), "h"),
            low=_decimal(message.get("l"), "l"),
            close=_decimal(message.get("c"), "c"),
            volume=_decimal(message.get("v"), "v"),
        )
        try:
            update.validate()
        except ValueError as exc:
            self._quarantine("INVALID_ALPACA_BAR_UPDATE")
            raise AlpacaMarketDataProtocolError(str(exc)) from exc
        self.last_bar_received_at = received_at
        self.last_bar_close_time = (
            close_time
            if self.last_bar_close_time is None
            else max(self.last_bar_close_time, close_time)
        )
        return update

    def _quarantine(self, reason: str) -> None:
        self.state = AlpacaMarketDataStreamState.QUARANTINED
        self._reasons.add(reason)

    @staticmethod
    def _decode_frame(raw_frame: bytes | str) -> tuple[Mapping[str, object], ...]:
        try:
            document = json.loads(
                raw_frame.decode("utf-8") if isinstance(raw_frame, bytes) else raw_frame
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AlpacaMarketDataProtocolError("invalid market-data JSON frame") from exc
        if not isinstance(document, list) or not document:
            raise AlpacaMarketDataProtocolError("market-data frame must be a non-empty list")
        messages: list[Mapping[str, object]] = []
        for item in document:
            if not isinstance(item, Mapping):
                raise AlpacaMarketDataProtocolError("market-data message must be an object")
            messages.append(item)
        return tuple(messages)

    @staticmethod
    def _string_set(value: object, name: str) -> set[str]:
        if not isinstance(value, list):
            raise AlpacaMarketDataProtocolError(f"subscription field {name} must be a list")
        result: set[str] = set()
        for item in value:
            text = str(item).strip().upper()
            if not text:
                raise AlpacaMarketDataProtocolError(f"subscription field {name} contains blank symbol")
            result.add(text)
        return result


class AlpacaLiveMarketDataSession:
    """One read-only live session. It emits provisional updates; F22C finalizes them."""

    def __init__(
        self,
        *,
        stream: AlpacaStockMarketDataStream,
        sink: AlpacaBarUpdateSink,
        socket_factory: MarketDataSocketFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.stream = stream
        self.sink = sink
        self.socket_factory = socket_factory
        self.clock = (lambda: datetime.now(UTC)) if clock is None else clock

    async def run_once(
        self,
        *,
        maximum_market_frames: int | None = None,
    ) -> AlpacaMarketDataSessionResult:
        if maximum_market_frames is not None and maximum_market_frames < 1:
            raise ValueError("maximum_market_frames must be positive when provided")
        factory = self.socket_factory or self._default_socket_factory()
        frames_processed = 0
        updates_delivered = 0
        try:
            async with AsyncExitStack() as stack:
                try:
                    socket = await stack.enter_async_context(factory(self.stream.endpoint.stream_url))
                except Exception as exc:
                    self.stream.degrade("MARKET_DATA_CONNECT_FAILURE")
                    raise AlpacaMarketDataTransportError("market-data connection failed") from exc
                await self._receive_handshake(socket, AlpacaMarketDataStreamState.CONNECTED)
                await self._send(socket, self.stream.authentication_frame())
                await self._receive_handshake(socket, AlpacaMarketDataStreamState.AUTHENTICATED)
                await self._send(socket, self.stream.subscription_frame())
                await self._receive_handshake(socket, AlpacaMarketDataStreamState.SUBSCRIBED)
                while maximum_market_frames is None or frames_processed < maximum_market_frames:
                    raw = await self._receive_market_frame(socket)
                    received_at = _aware(self.clock(), "clock")
                    updates = self.stream.ingest(
                        raw,
                        received_at=received_at,
                        expected_generation=self.stream.generation,
                    )
                    frames_processed += 1
                    for update in updates:
                        try:
                            self.sink.accept(update)
                        except Exception as exc:
                            self.stream.degrade("MARKET_DATA_SINK_FAILURE")
                            raise AlpacaMarketDataSinkError(
                                "market-data sink rejected update"
                            ) from exc
                        updates_delivered += 1
                evidence = self.stream.evidence(captured_at=_aware(self.clock(), "clock"))
            return AlpacaMarketDataSessionResult(updates_delivered, frames_processed, evidence)
        finally:
            self.stream.close()

    async def _send(self, socket: MarketDataSocket, message: bytes | str) -> None:
        try:
            await socket.send(message)
        except Exception as exc:
            self.stream.degrade("MARKET_DATA_TRANSPORT_FAILURE")
            raise AlpacaMarketDataTransportError("market-data send failed") from exc

    async def _receive_handshake(
        self,
        socket: MarketDataSocket,
        expected_state: AlpacaMarketDataStreamState,
    ) -> None:
        raw = await self._receive(
            socket,
            timeout=self.stream.policy.handshake_timeout,
            timeout_reason="MARKET_DATA_HANDSHAKE_TIMEOUT",
            timeout_message="market-data handshake timed out",
        )
        self.stream.ingest(
            raw,
            received_at=_aware(self.clock(), "clock"),
            expected_generation=self.stream.generation,
        )
        if self.stream.state is not expected_state:
            self.stream.quarantine("MARKET_DATA_HANDSHAKE_STATE_MISMATCH")
            raise AlpacaMarketDataProtocolError(
                f"expected handshake state {expected_state.value}, got {self.stream.state.value}"
            )

    async def _receive_market_frame(self, socket: MarketDataSocket) -> bytes | str:
        return await self._receive(
            socket,
            timeout=self.stream.policy.maximum_stream_silence,
            timeout_reason="MARKET_DATA_STREAM_SILENCE",
            timeout_message="market-data stream exceeded silence budget",
        )

    async def _receive(
        self,
        socket: MarketDataSocket,
        *,
        timeout: timedelta,
        timeout_reason: str,
        timeout_message: str,
    ) -> bytes | str:
        try:
            return await asyncio.wait_for(socket.recv(), timeout=timeout.total_seconds())
        except TimeoutError as exc:
            self.stream.degrade(timeout_reason)
            raise AlpacaMarketDataTransportError(timeout_message) from exc
        except Exception as exc:
            self.stream.degrade("MARKET_DATA_TRANSPORT_FAILURE")
            raise AlpacaMarketDataTransportError("market-data receive failed") from exc

    def _default_socket_factory(self) -> MarketDataSocketFactory:
        policy = self.stream.policy

        def factory(url: str) -> AbstractAsyncContextManager[MarketDataSocket]:
            try:
                from websockets.asyncio.client import connect
            except ImportError as exc:  # pragma: no cover - optional dependency boundary
                raise AlpacaMarketDataTransportError(
                    "install astra-trade-bot[marketdata] for live WebSocket support"
                ) from exc
            return connect(
                url,
                open_timeout=policy.websocket_open_timeout_seconds,
                ping_interval=policy.websocket_ping_interval_seconds,
                ping_timeout=policy.websocket_ping_timeout_seconds,
                max_size=policy.websocket_maximum_message_bytes,
                compression=None,
            )

        return factory
