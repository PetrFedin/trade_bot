from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import json
import os
import threading
import time
from typing import Any, Protocol

from app.runtime.paper_broker_contract_v99 import (
    BrokerAccount,
    BrokerMutationError,
    BrokerOrder,
    BrokerOrderStatus,
    OrderSide,
)
from app.runtime.platform_common_v90 import canonical_json, require_aware, sha256_digest

UTC = timezone.utc


class AlpacaPaperError(RuntimeError):
    pass


class AlpacaPaperConfigurationError(AlpacaPaperError):
    pass


class AlpacaPaperRateLimitExceeded(AlpacaPaperError):
    pass


class AlpacaPaperProtocolError(AlpacaPaperError):
    pass


class StaleStreamGeneration(AlpacaPaperError):
    pass


@dataclass(frozen=True)
class HttpResponseV100:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransportV100(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponseV100: ...


class AlpacaPaperCredentialsV100:
    __slots__ = ("_key_id", "_secret_key", "_fingerprint")

    KEY_ENV = "ASTRA_ALPACA_PAPER_KEY_ID"
    SECRET_ENV = "ASTRA_ALPACA_PAPER_SECRET_KEY"

    def __init__(self, *, key_id: str, secret_key: str) -> None:
        key_id, secret_key = key_id.strip(), secret_key.strip()
        if not key_id or not secret_key:
            raise AlpacaPaperConfigurationError("paper credentials are required")
        if any(value.isspace() for value in key_id + secret_key):
            raise AlpacaPaperConfigurationError("paper credentials cannot contain whitespace")
        self._key_id = key_id
        self._secret_key = secret_key
        self._fingerprint = sha256_digest(
            {"provider": "alpaca-paper", "key_id": key_id, "secret_key": secret_key}
        )[:16]

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "AlpacaPaperCredentialsV100":
        source = os.environ if environ is None else environ
        try:
            return cls(key_id=source[cls.KEY_ENV], secret_key=source[cls.SECRET_ENV])
        except KeyError as exc:
            raise AlpacaPaperConfigurationError(
                f"missing required credential environment variable: {exc.args[0]}"
            ) from exc

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def rest_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._key_id,
            "APCA-API-SECRET-KEY": self._secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ASTRA/7.30.0 paper-only",
        }

    def websocket_auth_document(self) -> dict[str, str]:
        return {"action": "auth", "key": self._key_id, "secret": self._secret_key}

    def __repr__(self) -> str:
        return f"AlpacaPaperCredentialsV100(fingerprint={self.fingerprint!r}, redacted=True)"


@dataclass(frozen=True)
class AlpacaPaperEndpointsV100:
    rest_base_url: str = "https://paper-api.alpaca.markets"
    stream_url: str = "wss://paper-api.alpaca.markets/stream"

    def validate(self) -> None:
        if self.rest_base_url.rstrip("/") != "https://paper-api.alpaca.markets":
            raise AlpacaPaperConfigurationError("REST endpoint must be Alpaca paper")
        if self.stream_url != "wss://paper-api.alpaca.markets/stream":
            raise AlpacaPaperConfigurationError("stream endpoint must be Alpaca paper")


@dataclass(frozen=True)
class AlpacaPaperPolicyV100:
    maximum_read_attempts: int = 3
    initial_backoff_seconds: float = 0.05
    maximum_backoff_seconds: float = 0.5
    read_capacity: int = 200
    read_refill_per_second: float = 200 / 60
    mutation_capacity: int = 20
    mutation_refill_per_second: float = 20 / 60
    timeout_seconds: float = 10.0

    def validate(self) -> None:
        if self.maximum_read_attempts < 1:
            raise ValueError("maximum_read_attempts must be positive")
        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds cannot be negative")
        if self.maximum_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("maximum_backoff_seconds is too small")
        if min(self.read_capacity, self.mutation_capacity) < 1:
            raise ValueError("rate-limit capacities must be positive")
        if min(self.read_refill_per_second, self.mutation_refill_per_second) <= 0:
            raise ValueError("rate-limit refill rates must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


class TokenBucketV100:
    def __init__(
        self,
        *,
        capacity: int,
        refill_per_second: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("invalid token bucket configuration")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self.clock = clock
        self._tokens = float(capacity)
        self._updated_at = float(clock())
        self._lock = threading.RLock()

    def try_acquire(self) -> bool:
        with self._lock:
            now = float(self.clock())
            self._tokens = min(
                self.capacity,
                self._tokens + max(0.0, now - self._updated_at) * self.refill_per_second,
            )
            self._updated_at = now
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True


_STATUS_MAP = {
    "new": BrokerOrderStatus.ACKNOWLEDGED,
    "accepted": BrokerOrderStatus.ACKNOWLEDGED,
    "pending_new": BrokerOrderStatus.ACKNOWLEDGED,
    "replaced": BrokerOrderStatus.REPLACED,
    "canceled": BrokerOrderStatus.CANCELLED,
    "cancelled": BrokerOrderStatus.CANCELLED,
    "expired": BrokerOrderStatus.CANCELLED,
    "rejected": BrokerOrderStatus.REJECTED,
    "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
    "filled": BrokerOrderStatus.FILLED,
}


class AlpacaPaperAdapterV100:
    """Paper-only REST adapter with read retries and single-attempt mutations."""

    def __init__(
        self,
        *,
        credentials: AlpacaPaperCredentialsV100,
        transport: HttpTransportV100,
        endpoints: AlpacaPaperEndpointsV100 = AlpacaPaperEndpointsV100(),
        policy: AlpacaPaperPolicyV100 = AlpacaPaperPolicyV100(),
        paper_order_writes_enabled: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        endpoints.validate()
        policy.validate()
        self.credentials = credentials
        self.transport = transport
        self.endpoints = endpoints
        self.policy = policy
        self.paper_order_writes_enabled = bool(paper_order_writes_enabled)
        self.sleeper = sleeper
        self._read_limiter = TokenBucketV100(
            capacity=policy.read_capacity,
            refill_per_second=policy.read_refill_per_second,
            clock=clock,
        )
        self._mutation_limiter = TokenBucketV100(
            capacity=policy.mutation_capacity,
            refill_per_second=policy.mutation_refill_per_second,
            clock=clock,
        )

    def __repr__(self) -> str:
        return (
            "AlpacaPaperAdapterV100("
            f"credentials_fingerprint={self.credentials.fingerprint!r}, "
            f"paper_order_writes_enabled={self.paper_order_writes_enabled!r})"
        )

    def get_account(self) -> BrokerAccount:
        return self._parse_account(self._read("GET", "/v2/account"))

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        raw = self._read("GET", "/v2/orders?status=open&nested=false")
        if not isinstance(raw, list):
            raise AlpacaPaperProtocolError("open-orders response must be a list")
        return tuple(self._parse_order(value) for value in raw)

    def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        try:
            raw = self._read(
                "GET", f"/v2/orders:by_client_order_id?client_order_id={client_order_id}"
            )
        except BrokerMutationError as exc:
            if exc.code == "404":
                return None
            raise
        return self._parse_order(raw)

    def submit_limit_order(
        self,
        *,
        client_order_id: str,
        instrument: str,
        side: OrderSide,
        quantity: Decimal,
        limit_price: Decimal,
    ) -> BrokerOrder:
        payload = {
            "client_order_id": client_order_id,
            "symbol": instrument,
            "side": side.value.lower(),
            "qty": str(quantity),
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(limit_price),
        }
        return self._parse_order(self._mutate("POST", "/v2/orders", payload))

    def replace_limit_order(
        self, *, broker_order_id: str, limit_price: Decimal
    ) -> BrokerOrder:
        return self._parse_order(
            self._mutate(
                "PATCH", f"/v2/orders/{broker_order_id}", {"limit_price": str(limit_price)}
            )
        )

    def cancel_order(self, *, broker_order_id: str) -> BrokerOrder:
        self._mutate("DELETE", f"/v2/orders/{broker_order_id}", None, allow_empty=True)
        raw = self._read("GET", f"/v2/orders/{broker_order_id}")
        return self._parse_order(raw)

    def _read(self, method: str, path: str) -> object:
        if not self._read_limiter.try_acquire():
            raise AlpacaPaperRateLimitExceeded("local paper read rate limit exceeded")
        delay = self.policy.initial_backoff_seconds
        for attempt in range(1, self.policy.maximum_read_attempts + 1):
            try:
                return self._request(method, path, None)
            except BrokerMutationError as exc:
                if not exc.ambiguous or attempt == self.policy.maximum_read_attempts:
                    raise
                self.sleeper(delay)
                delay = min(self.policy.maximum_backoff_seconds, max(delay * 2, delay))
        raise AssertionError("unreachable")

    def _mutate(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None,
        *,
        allow_empty: bool = False,
    ) -> object:
        if not self.paper_order_writes_enabled:
            raise BrokerMutationError("WRITES_DISABLED", "paper order writes are disabled")
        if not self._mutation_limiter.try_acquire():
            raise AlpacaPaperRateLimitExceeded("local paper mutation rate limit exceeded")
        return self._request(method, path, payload, allow_empty=allow_empty)

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None,
        *,
        allow_empty: bool = False,
    ) -> object:
        body = None if payload is None else canonical_json(payload)
        try:
            response = self.transport.request(
                method,
                self.endpoints.rest_base_url.rstrip("/") + path,
                headers=self.credentials.rest_headers(),
                body=body,
                timeout_seconds=self.policy.timeout_seconds,
            )
        except (TimeoutError, OSError) as exc:
            raise BrokerMutationError("TRANSPORT", str(exc), ambiguous=True) from exc
        if response.status == 204 and allow_empty:
            return {}
        try:
            document = json.loads(response.body.decode("utf-8")) if response.body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AlpacaPaperProtocolError("invalid broker JSON response") from exc
        if response.status < 200 or response.status >= 300:
            code = str(document.get("code", response.status)) if isinstance(document, Mapping) else str(response.status)
            message = str(document.get("message", "broker request failed")) if isinstance(document, Mapping) else "broker request failed"
            ambiguous = response.status in {408, 425, 429} or response.status >= 500
            raise BrokerMutationError(code, message, ambiguous=ambiguous)
        return document

    @staticmethod
    def _decimal(value: object, field: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise AlpacaPaperProtocolError(f"invalid decimal field: {field}") from exc
        if not parsed.is_finite():
            raise AlpacaPaperProtocolError(f"non-finite decimal field: {field}")
        return parsed

    @classmethod
    def _parse_account(cls, value: object) -> BrokerAccount:
        if not isinstance(value, Mapping):
            raise AlpacaPaperProtocolError("account response must be an object")
        account = BrokerAccount(
            account_id=str(value.get("id", "")),
            status=str(value.get("status", "")),
            currency=str(value.get("currency", "")),
            buying_power=cls._decimal(value.get("buying_power", "0"), "buying_power"),
            trading_blocked=bool(value.get("trading_blocked", False)),
        )
        account.validate()
        return account

    @classmethod
    def _parse_order(cls, value: object) -> BrokerOrder:
        if not isinstance(value, Mapping):
            raise AlpacaPaperProtocolError("order response must be an object")
        status_name = str(value.get("status", "")).lower()
        status = _STATUS_MAP.get(status_name)
        if status is None:
            raise AlpacaPaperProtocolError(f"unsupported broker order status: {status_name}")
        side_name = str(value.get("side", "")).upper()
        try:
            side = OrderSide(side_name)
        except ValueError as exc:
            raise AlpacaPaperProtocolError(f"unsupported order side: {side_name}") from exc
        updated_raw = str(value.get("updated_at") or value.get("submitted_at") or "")
        try:
            updated_at = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AlpacaPaperProtocolError("invalid order updated_at") from exc
        order = BrokerOrder(
            client_order_id=str(value.get("client_order_id", "")),
            broker_order_id=str(value.get("id", "")),
            instrument=str(value.get("symbol", "")).upper(),
            side=side,
            quantity=cls._decimal(value.get("qty", "0"), "qty"),
            limit_price=cls._decimal(value.get("limit_price", "0"), "limit_price"),
            status=status,
            filled_quantity=cls._decimal(value.get("filled_qty", "0"), "filled_qty"),
            updated_at=require_aware(updated_at, field_name="updated_at").astimezone(UTC),
        )
        order.validate()
        return order


class TradeStreamStateV100(str, Enum):
    CONNECTING = "CONNECTING"
    AUTHENTICATING = "AUTHENTICATING"
    AUTHORIZED = "AUTHORIZED"
    LISTENING = "LISTENING"
    DEGRADED = "DEGRADED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class TradeUpdateV100:
    sequence: int
    event: str
    order: BrokerOrder
    received_at: datetime
    digest: str


class AlpacaTradeUpdateStreamV100:
    ALLOWED_EVENTS = frozenset(
        {
            "accepted", "new", "pending_new", "partial_fill", "fill",
            "pending_cancel", "canceled", "expired", "done_for_day",
            "pending_replace", "replaced", "rejected", "stopped", "calculated",
            "suspended", "order_replace_rejected", "order_cancel_rejected",
        }
    )

    def __init__(
        self,
        *,
        generation: int,
        credentials: AlpacaPaperCredentialsV100,
        maximum_silence: timedelta = timedelta(seconds=45),
        endpoints: AlpacaPaperEndpointsV100 = AlpacaPaperEndpointsV100(),
    ) -> None:
        if generation <= 0:
            raise ValueError("generation must be positive")
        if maximum_silence <= timedelta(0):
            raise ValueError("maximum_silence must be positive")
        endpoints.validate()
        self.generation = generation
        self.credentials = credentials
        self.maximum_silence = maximum_silence
        self.endpoints = endpoints
        self.state = TradeStreamStateV100.CONNECTING
        self.last_message_at: datetime | None = None
        self.last_trade_update_at: datetime | None = None
        self.accepted_updates = 0
        self.duplicate_updates = 0
        self._seen: set[str] = set()
        self._versions: dict[str, tuple[Decimal, datetime]] = {}
        self._reasons: set[str] = set()
        self._lock = threading.RLock()

    def authentication_frame(self) -> bytes:
        self.state = TradeStreamStateV100.AUTHENTICATING
        return canonical_json(self.credentials.websocket_auth_document())

    @staticmethod
    def listen_frame() -> bytes:
        return canonical_json({"action": "listen", "data": {"streams": ["trade_updates"]}})

    def ingest(
        self,
        raw_frame: bytes | str,
        *,
        received_at: datetime,
        expected_generation: int,
    ) -> TradeUpdateV100 | None:
        received_at = require_aware(received_at, field_name="received_at").astimezone(UTC)
        if expected_generation != self.generation:
            raise StaleStreamGeneration("stale Alpaca stream generation")
        with self._lock:
            document = self._decode(raw_frame)
            self.last_message_at = received_at if self.last_message_at is None else max(self.last_message_at, received_at)
            digest = sha256_digest(document)
            if digest in self._seen:
                self.duplicate_updates += 1
                return None
            self._seen.add(digest)
            stream = str(document.get("stream", ""))
            if stream == "authorization":
                data = self._mapping(document.get("data"), "authorization data")
                if str(data.get("status", "")).lower() != "authorized":
                    self._quarantine("UNAUTHORIZED")
                    raise AlpacaPaperProtocolError("stream authorization failed")
                self.state = TradeStreamStateV100.AUTHORIZED
                return None
            if stream == "listening":
                data = self._mapping(document.get("data"), "listening data")
                if self.state is not TradeStreamStateV100.AUTHORIZED:
                    self._quarantine("LISTEN_BEFORE_AUTHORIZATION")
                    raise AlpacaPaperProtocolError("listen before authorization")
                if "trade_updates" not in data.get("streams", []):
                    self._quarantine("TRADE_UPDATES_NOT_LISTENING")
                    raise AlpacaPaperProtocolError("trade_updates not acknowledged")
                self.state = TradeStreamStateV100.LISTENING
                return None
            if stream == "trade_updates":
                if self.state is not TradeStreamStateV100.LISTENING:
                    self._quarantine("UPDATE_BEFORE_LISTENING")
                    raise AlpacaPaperProtocolError("update before listening")
                data = self._mapping(document.get("data"), "trade update data")
                event = str(data.get("event", "")).lower()
                if event not in self.ALLOWED_EVENTS:
                    self._quarantine("UNKNOWN_TRADE_EVENT")
                    raise AlpacaPaperProtocolError(f"unsupported trade event: {event}")
                order = AlpacaPaperAdapterV100._parse_order(data.get("order"))
                prior = self._versions.get(order.client_order_id)
                if prior is not None:
                    if order.filled_quantity < prior[0]:
                        self._quarantine("FILLED_QUANTITY_REGRESSION")
                        raise AlpacaPaperProtocolError("filled quantity regressed")
                    if order.updated_at < prior[1]:
                        self._quarantine("BROKER_TIME_REGRESSION")
                        raise AlpacaPaperProtocolError("broker time regressed")
                self._versions[order.client_order_id] = (order.filled_quantity, order.updated_at)
                self.accepted_updates += 1
                self.last_trade_update_at = received_at if self.last_trade_update_at is None else max(self.last_trade_update_at, received_at)
                return TradeUpdateV100(self.accepted_updates, event, order, received_at, digest)
            if document.get("action") == "error":
                self.state = TradeStreamStateV100.DEGRADED
                self._reasons.add("STREAM_ERROR")
                return None
            self._quarantine("UNKNOWN_STREAM_MESSAGE")
            raise AlpacaPaperProtocolError("unknown stream message")

    def evidence(self, *, captured_at: datetime) -> Mapping[str, object]:
        captured_at = require_aware(captured_at, field_name="captured_at").astimezone(UTC)
        reasons = set(self._reasons)
        if self.state is not TradeStreamStateV100.LISTENING:
            reasons.add("STREAM_NOT_LISTENING")
        if self.last_message_at is None:
            reasons.add("NO_STREAM_MESSAGES")
        elif captured_at - self.last_message_at > self.maximum_silence:
            reasons.add("STREAM_STALE")
        document = {
            "generation": self.generation,
            "state": self.state.value,
            "captured_at": captured_at.isoformat(),
            "last_message_at": None if self.last_message_at is None else self.last_message_at.isoformat(),
            "last_trade_update_at": None if self.last_trade_update_at is None else self.last_trade_update_at.isoformat(),
            "accepted_updates": self.accepted_updates,
            "duplicate_updates": self.duplicate_updates,
            "ready": not reasons,
            "reasons": tuple(sorted(reasons)),
            "credentials_fingerprint": self.credentials.fingerprint,
            "external_order_routing_allowed": False,
            "live_trading_allowed": False,
        }
        return {**document, "digest": sha256_digest(document)}

    def _quarantine(self, reason: str) -> None:
        self.state = TradeStreamStateV100.QUARANTINED
        self._reasons.add(reason)

    @staticmethod
    def _mapping(value: object, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise AlpacaPaperProtocolError(f"{name} must be an object")
        return value

    @staticmethod
    def _decode(raw_frame: bytes | str) -> Mapping[str, Any]:
        try:
            value = json.loads(raw_frame.decode("utf-8") if isinstance(raw_frame, bytes) else raw_frame)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AlpacaPaperProtocolError("invalid JSON stream frame") from exc
        if not isinstance(value, Mapping):
            raise AlpacaPaperProtocolError("stream frame must be an object")
        return value
