from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Protocol

_BYBIT_DEMO_PRIVATE_WS = "wss://stream-demo.bybit.com/v5/private"
_TOPICS = ("order", "execution", "position")


class BybitPrivateStreamError(RuntimeError):
    pass


class BybitPrivateStreamProtocolError(BybitPrivateStreamError):
    pass


class BybitPrivateStreamDependencyUnavailable(BybitPrivateStreamError):
    pass


class BybitPrivateStreamConnection(Protocol):
    def send(self, message: str | bytes) -> None: ...

    def recv(self, timeout: float | None = None) -> str | bytes: ...

    def close(self) -> None: ...


class BybitPrivateStreamConnector(Protocol):
    live_mainnet_order_routing_allowed: bool

    def __call__(
        self,
        url: str,
        *,
        timeout_seconds: float,
    ) -> BybitPrivateStreamConnection: ...


class BybitPrivateWebsocketsConnector:
    live_mainnet_order_routing_allowed = False

    def __call__(
        self,
        url: str,
        *,
        timeout_seconds: float,
    ) -> BybitPrivateStreamConnection:
        if url != _BYBIT_DEMO_PRIVATE_WS:
            raise BybitPrivateStreamProtocolError(
                "private stream connector is restricted to Bybit demo"
            )
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise BybitPrivateStreamDependencyUnavailable(
                "websockets is required for the canonical Bybit product"
            ) from exc
        return connect(
            url,
            open_timeout=timeout_seconds,
            close_timeout=timeout_seconds,
            ping_interval=None,
            ping_timeout=None,
            max_size=1_048_576,
            max_queue=32,
        )


@dataclass(frozen=True)
class BybitPrivateStreamPolicy:
    connect_timeout_seconds: float = 10.0
    auth_expiry_ms: int = 10_000
    heartbeat_seconds: float = 20.0
    stale_after_seconds: float = 45.0
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    shutdown_join_seconds: float = 5.0

    def validate(self) -> None:
        if self.connect_timeout_seconds <= 0:
            raise ValueError("private stream connect timeout must be positive")
        if self.auth_expiry_ms < 1000:
            raise ValueError("private stream auth expiry must be at least one second")
        if self.heartbeat_seconds <= 0:
            raise ValueError("private stream heartbeat must be positive")
        if self.stale_after_seconds <= self.heartbeat_seconds:
            raise ValueError("private stream stale threshold must exceed heartbeat interval")
        if self.reconnect_initial_seconds <= 0:
            raise ValueError("private stream reconnect delay must be positive")
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError("private stream reconnect max must not be below initial delay")
        if self.shutdown_join_seconds <= 0:
            raise ValueError("private stream shutdown join must be positive")


@dataclass(frozen=True)
class BybitPrivateStreamSnapshot:
    running: bool
    connected: bool
    authenticated: bool
    subscribed: bool
    healthy: bool
    generation: int
    reconciliation_required: bool
    reconciliation_token: int
    last_message_monotonic: float | None
    last_error_type: str | None
    live_mainnet_order_routing_allowed: bool = False


ClockMs = Callable[[], int]
MonotonicFn = Callable[[], float]


class BybitPrivateStreamMonitor:
    """Reactive private-stream health signal; REST remains broker truth.

    WebSocket messages never mutate trading state. Every trade-relevant message and every reconnect
    creates a reconciliation token that must be acknowledged only after a fresh REST reconciliation.
    """

    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        url: str = _BYBIT_DEMO_PRIVATE_WS,
        connector: BybitPrivateStreamConnector | None = None,
        policy: BybitPrivateStreamPolicy | None = None,
        clock_ms: ClockMs | None = None,
        monotonic_fn: MonotonicFn = time.monotonic,
    ) -> None:
        if not api_key.strip() or not api_secret.strip():
            raise ValueError("private stream credentials are required")
        if url != _BYBIT_DEMO_PRIVATE_WS:
            raise ValueError("private stream URL must remain Bybit demo")
        active_policy = BybitPrivateStreamPolicy() if policy is None else policy
        active_policy.validate()
        active_connector = BybitPrivateWebsocketsConnector() if connector is None else connector
        if getattr(active_connector, "live_mainnet_order_routing_allowed", True) is not False:
            raise ValueError("private stream rejected mainnet-capable connector")
        self._api_key = api_key
        self._api_secret = api_secret
        self._url = url
        self._connector = active_connector
        self._policy = active_policy
        self._clock_ms = (
            (lambda: int(time.time() * 1000)) if clock_ms is None else clock_ms
        )
        self._monotonic = monotonic_fn
        self._stop = Event()
        self._wake = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._connection: BybitPrivateStreamConnection | None = None
        self._running = False
        self._connected = False
        self._authenticated = False
        self._subscribed = False
        self._generation = 0
        self._reconciliation_required = False
        self._reconciliation_token = 0
        self._last_message_monotonic: float | None = None
        self._last_error_type: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._stop.clear()
            self._wake.clear()
            self._running = True
            self._thread = Thread(
                target=self._run,
                name="astra-bybit-private-stream",
                daemon=True,
            )
            thread = self._thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            connection = self._connection
            thread = self._thread
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - shutdown continues after transport close failure.
                pass
        if thread is not None:
            thread.join(timeout=self._policy.shutdown_join_seconds)
        with self._lock:
            self._running = False
            self._connected = False
            self._authenticated = False
            self._subscribed = False
            self._connection = None

    def wait(self, timeout_seconds: float) -> None:
        if timeout_seconds < 0:
            raise ValueError("private stream wait timeout cannot be negative")
        self._wake.wait(timeout_seconds)
        self._wake.clear()

    def snapshot(self) -> BybitPrivateStreamSnapshot:
        now = self._monotonic()
        with self._lock:
            last_message = self._last_message_monotonic
            healthy = (
                self._running
                and self._connected
                and self._authenticated
                and self._subscribed
                and last_message is not None
                and now - last_message <= self._policy.stale_after_seconds
            )
            return BybitPrivateStreamSnapshot(
                running=self._running,
                connected=self._connected,
                authenticated=self._authenticated,
                subscribed=self._subscribed,
                healthy=healthy,
                generation=self._generation,
                reconciliation_required=self._reconciliation_required,
                reconciliation_token=self._reconciliation_token,
                last_message_monotonic=last_message,
                last_error_type=self._last_error_type,
            )

    def acknowledge_reconciliation(self, *, token: int) -> bool:
        if isinstance(token, bool) or token <= 0:
            raise ValueError("private stream reconciliation token must be positive")
        with self._lock:
            if token != self._reconciliation_token:
                return False
            self._reconciliation_required = False
            return True

    def _run(self) -> None:
        failures = 0
        try:
            while not self._stop.is_set():
                connection: BybitPrivateStreamConnection | None = None
                try:
                    connection = self._connector(
                        self._url,
                        timeout_seconds=self._policy.connect_timeout_seconds,
                    )
                    with self._lock:
                        self._connection = connection
                        self._connected = True
                        self._authenticated = False
                        self._subscribed = False
                        self._last_error_type = None
                    self._authenticate(connection)
                    self._subscribe(connection)
                    now = self._monotonic()
                    with self._lock:
                        self._authenticated = True
                        self._subscribed = True
                        self._generation += 1
                        self._last_message_monotonic = now
                        self._require_reconciliation_locked()
                    self._wake.set()
                    failures = 0
                    self._receive_loop(connection)
                except Exception as exc:  # noqa: BLE001 - any stream fault degrades to REST-only.
                    if self._stop.is_set():
                        break
                    failures += 1
                    self._mark_disconnected(type(exc).__name__)
                    delay = min(
                        self._policy.reconnect_initial_seconds * (2 ** min(failures - 1, 10)),
                        self._policy.reconnect_max_seconds,
                    )
                    self._stop.wait(delay)
                finally:
                    if connection is not None:
                        try:
                            connection.close()
                        except Exception:  # noqa: BLE001 - reconnect path must continue.
                            pass
                    with self._lock:
                        if self._connection is connection:
                            self._connection = None
        finally:
            with self._lock:
                self._running = False
                self._connected = False
                self._authenticated = False
                self._subscribed = False
                self._connection = None
            self._wake.set()

    def _authenticate(self, connection: BybitPrivateStreamConnection) -> None:
        expires = self._clock_ms() + self._policy.auth_expiry_ms
        if isinstance(expires, bool) or expires <= 0:
            raise ValueError("private stream clock returned an invalid timestamp")
        signature = _auth_signature(self._api_secret, expires)
        connection.send(
            json.dumps(
                {"op": "auth", "args": [self._api_key, expires, signature]},
                separators=(",", ":"),
            )
        )
        response = _decode_mapping(
            connection.recv(timeout=self._policy.connect_timeout_seconds)
        )
        if response.get("op") != "auth" or response.get("success") is not True:
            raise BybitPrivateStreamProtocolError("private stream authentication failed")
        with self._lock:
            self._authenticated = True
            self._last_message_monotonic = self._monotonic()

    def _subscribe(self, connection: BybitPrivateStreamConnection) -> None:
        connection.send(
            json.dumps(
                {"op": "subscribe", "args": list(_TOPICS)},
                separators=(",", ":"),
            )
        )
        response = _decode_mapping(
            connection.recv(timeout=self._policy.connect_timeout_seconds)
        )
        if response.get("op") != "subscribe" or response.get("success") is not True:
            raise BybitPrivateStreamProtocolError("private stream subscription failed")
        with self._lock:
            self._subscribed = True
            self._last_message_monotonic = self._monotonic()

    def _receive_loop(self, connection: BybitPrivateStreamConnection) -> None:
        last_ping = self._monotonic()
        while not self._stop.is_set():
            now = self._monotonic()
            timeout = max(0.1, self._policy.heartbeat_seconds - (now - last_ping))
            try:
                raw = connection.recv(timeout=timeout)
            except TimeoutError:
                now = self._monotonic()
                with self._lock:
                    last_message = self._last_message_monotonic
                if (
                    last_message is None
                    or now - last_message > self._policy.stale_after_seconds
                ):
                    raise BybitPrivateStreamError("private stream heartbeat is stale")
                connection.send(json.dumps({"op": "ping"}, separators=(",", ":")))
                last_ping = now
                continue
            frame = _decode_mapping(raw)
            received = self._monotonic()
            with self._lock:
                self._last_message_monotonic = received
                self._last_error_type = None
            if _is_heartbeat_frame(frame):
                continue
            self._handle_topic_frame(frame)
            if received - last_ping >= self._policy.heartbeat_seconds:
                connection.send(json.dumps({"op": "ping"}, separators=(",", ":")))
                last_ping = received

    def _handle_topic_frame(self, frame: Mapping[str, object]) -> None:
        topic = frame.get("topic")
        if topic not in _TOPICS:
            raise BybitPrivateStreamProtocolError("unexpected private stream topic")
        data = frame.get("data")
        if not isinstance(data, list):
            raise BybitPrivateStreamProtocolError("private stream topic data must be a list")
        for row in data:
            if not isinstance(row, Mapping):
                raise BybitPrivateStreamProtocolError(
                    "private stream topic row must be an object"
                )
            _normalized_symbol(row.get("symbol"))
            if topic in {"execution", "position"}:
                seq = _sequence(row.get("seq"))
                if seq < -1:
                    raise BybitPrivateStreamProtocolError(
                        "private stream sequence is invalid"
                    )
        with self._lock:
            self._require_reconciliation_locked()
        self._wake.set()

    def _mark_disconnected(self, error_type: str) -> None:
        with self._lock:
            self._connected = False
            self._authenticated = False
            self._subscribed = False
            self._last_error_type = error_type
            self._require_reconciliation_locked()
        self._wake.set()

    def _require_reconciliation_locked(self) -> None:
        self._reconciliation_required = True
        self._reconciliation_token += 1


def _auth_signature(api_secret: str, expires_ms: int) -> str:
    if not api_secret:
        raise ValueError("private stream API secret is required")
    if isinstance(expires_ms, bool) or expires_ms <= 0:
        raise ValueError("private stream expiry must be positive")
    payload = f"GET/realtime{expires_ms}".encode()
    return hmac.new(api_secret.encode(), payload, hashlib.sha256).hexdigest()


def _decode_mapping(raw: str | bytes) -> Mapping[str, object]:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise BybitPrivateStreamProtocolError("invalid private stream JSON") from exc
    if not isinstance(payload, Mapping):
        raise BybitPrivateStreamProtocolError("private stream frame must be an object")
    return payload


def _is_heartbeat_frame(frame: Mapping[str, object]) -> bool:
    op = frame.get("op")
    if op == "pong":
        return True
    return op == "ping" and frame.get("success") is True and frame.get("ret_msg") == "pong"


def _normalized_symbol(value: object) -> str:
    if not isinstance(value, str):
        raise BybitPrivateStreamProtocolError("private stream symbol is missing")
    symbol = value.strip().upper()
    if value != symbol or not symbol.endswith("USDT") or not symbol[:-4].isalnum():
        raise BybitPrivateStreamProtocolError("private stream symbol is invalid")
    return symbol


def _sequence(value: object) -> int:
    if isinstance(value, bool):
        raise BybitPrivateStreamProtocolError("private stream sequence is invalid")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BybitPrivateStreamProtocolError("private stream sequence is invalid") from exc
