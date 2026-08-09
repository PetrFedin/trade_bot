from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import socket
import ssl
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from app.runtime.sandbox_qualification_v101 import (
    AccountSnapshot,
    AmbiguousMutation,
    OrderSnapshot,
    OrderStatus,
    PaperGateway,
    Side,
    StreamEvidence,
    require_aware,
)

UTC = timezone.utc
PAPER_REST = "https://paper-api.alpaca.markets"
PAPER_STREAM = "wss://paper-api.alpaca.markets/stream"


class ExternalProbeError(RuntimeError):
    pass


class ConfigurationError(ExternalProbeError):
    pass


class ProtocolError(ExternalProbeError):
    pass


class DependencyUnavailable(ExternalProbeError):
    pass


@dataclass(frozen=True, repr=False)
class Credentials:
    key_id: str
    secret_key: str

    KEY_ENV = "ASTRA_ALPACA_PAPER_KEY_ID"
    SECRET_ENV = "ASTRA_ALPACA_PAPER_SECRET_KEY"

    def __post_init__(self) -> None:
        if not self.key_id.strip() or not self.secret_key.strip():
            raise ValueError("paper credentials cannot be empty")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "Credentials":
        source = os.environ if environment is None else environment
        missing = [name for name in (cls.KEY_ENV, cls.SECRET_ENV) if not source.get(name, "").strip()]
        if missing:
            raise ConfigurationError(f"missing paper credential variables: {','.join(missing)}")
        return cls(source[cls.KEY_ENV], source[cls.SECRET_ENV])

    @property
    def fingerprint(self) -> str:
        material = f"{self.key_id}\0{self.secret_key}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:16]

    def headers(self) -> Mapping[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "astra-schema101-sandbox-qualification/7.31.0",
        }

    def __repr__(self) -> str:
        return f"Credentials(fingerprint={self.fingerprint!r}, redacted=True)"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        body: bytes | None, timeout_seconds: float,
    ) -> HttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class UrllibTransport:
    """TLS-verifying, redirect-rejecting transport pinned to Alpaca paper REST."""

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        self.context = ssl.create_default_context() if context is None else context
        if self.context.verify_mode != ssl.CERT_REQUIRED or not self.context.check_hostname:
            raise ValueError("TLS hostname and certificate verification must remain enabled")
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=self.context))

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        body: bytes | None, timeout_seconds: float,
    ) -> HttpResponse:
        if not url.startswith(f"{PAPER_REST}/"):
            raise ProtocolError("transport is restricted to Alpaca paper REST")
        request = Request(url=url, data=body, method=method.upper(), headers=dict(headers))
        try:
            with self.opener.open(request, timeout=timeout_seconds) as response:
                return HttpResponse(int(response.status), dict(response.headers.items()), response.read())
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ProtocolError("HTTP redirects are forbidden") from exc
            return HttpResponse(int(exc.code), dict(exc.headers.items()) if exc.headers else {}, exc.read())
        except socket.timeout as exc:
            raise TimeoutError("paper REST request timed out") from exc
        except URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError("paper REST request timed out") from exc
            raise OSError(f"paper REST transport failure: {type(exc.reason).__name__}") from exc


@dataclass(frozen=True)
class GatewayPolicy:
    timeout_seconds: float = 10.0
    read_attempts: int = 3
    retry_backoff_seconds: float = 0.05

    def validate(self) -> None:
        if self.timeout_seconds <= 0 or self.read_attempts < 1 or self.retry_backoff_seconds < 0:
            raise ValueError("gateway policy is invalid")


_STATUS = {
    "new": OrderStatus.ACKNOWLEDGED,
    "accepted": OrderStatus.ACKNOWLEDGED,
    "pending_new": OrderStatus.ACKNOWLEDGED,
    "accepted_for_bidding": OrderStatus.ACKNOWLEDGED,
    "replaced": OrderStatus.REPLACED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
}


class AlpacaPaperGateway(PaperGateway):
    paper_only = True
    rest_endpoint = PAPER_REST
    stream_endpoint = PAPER_STREAM

    def __init__(
        self, *, credentials: Credentials, transport: HttpTransport,
        writes_enabled: bool = False, policy: GatewayPolicy = GatewayPolicy(),
        sleeper=time.sleep,
    ) -> None:
        policy.validate()
        self.credentials = credentials
        self.transport = transport
        self.writes_enabled = writes_enabled
        self.policy = policy
        self.sleeper = sleeper
        self.credential_fingerprint = credentials.fingerprint

    def get_account(self) -> AccountSnapshot:
        payload = self._read("/v2/account")
        account = AccountSnapshot(
            account_id=str(payload["id"]),
            status=str(payload["status"]),
            currency=str(payload["currency"]),
            buying_power=Decimal(str(payload["buying_power"])),
            trading_blocked=bool(payload.get("trading_blocked", False)),
        )
        account.validate()
        return account

    def list_open_orders(self) -> Sequence[OrderSnapshot]:
        payload = self._read("/v2/orders?status=open&direction=asc&nested=false")
        if not isinstance(payload, list):
            raise ProtocolError("open orders response must be a list")
        return tuple(self._parse_order(item) for item in payload)

    def get_order_by_client_order_id(self, client_order_id: str) -> OrderSnapshot | None:
        try:
            payload = self._read(f"/v2/orders:by_client_order_id?client_order_id={quote(client_order_id, safe='')}")
        except ExternalProbeError as exc:
            if "HTTP_404" in str(exc):
                return None
            raise
        return self._parse_order(payload)

    def submit_limit_order(
        self, *, client_order_id: str, symbol: str, side: Side,
        quantity: Decimal, limit_price: Decimal,
    ) -> OrderSnapshot:
        return self._parse_order(self._mutation("POST", "/v2/orders", {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side.value.lower(),
            "qty": str(quantity),
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(limit_price),
        }))

    def replace_limit_order(self, *, broker_order_id: str, limit_price: Decimal) -> OrderSnapshot:
        return self._parse_order(self._mutation("PATCH", f"/v2/orders/{quote(broker_order_id, safe='')}", {
            "limit_price": str(limit_price),
        }))

    def cancel_order(self, *, broker_order_id: str) -> OrderSnapshot:
        self._mutation("DELETE", f"/v2/orders/{quote(broker_order_id, safe='')}", None, allow_empty=True)
        # A read-only lookup proves the resulting state; no mutation retry is attempted.
        payload = self._read(f"/v2/orders/{quote(broker_order_id, safe='')}")
        return self._parse_order(payload)

    def _read(self, path: str) -> object:
        last: Exception | None = None
        for attempt in range(1, self.policy.read_attempts + 1):
            try:
                response = self.transport.request(
                    "GET", self.rest_endpoint + path,
                    headers=self.credentials.headers(), body=None,
                    timeout_seconds=self.policy.timeout_seconds,
                )
                if response.status == 429 or 500 <= response.status < 600:
                    raise ExternalProbeError(f"HTTP_{response.status}")
                return self._decode(response)
            except (TimeoutError, OSError, ExternalProbeError) as exc:
                last = exc
                retryable = isinstance(exc, (TimeoutError, OSError)) or "HTTP_429" in str(exc) or "HTTP_5" in str(exc)
                if not retryable or attempt == self.policy.read_attempts:
                    raise
                self.sleeper(self.policy.retry_backoff_seconds * attempt)
        raise ExternalProbeError("read attempts exhausted") from last

    def _mutation(self, method: str, path: str, payload: Mapping[str, object] | None, *, allow_empty: bool = False) -> object:
        if not self.writes_enabled:
            raise ConfigurationError("paper mutations are disabled")
        body = None if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            response = self.transport.request(
                method, self.rest_endpoint + path,
                headers=self.credentials.headers(), body=body,
                timeout_seconds=self.policy.timeout_seconds,
            )
        except (TimeoutError, OSError) as exc:
            raise AmbiguousMutation(f"{method} transport outcome is ambiguous") from exc
        if response.status == 429 or 500 <= response.status < 600:
            raise AmbiguousMutation(f"{method} HTTP_{response.status} outcome is ambiguous")
        if allow_empty and response.status in {200, 202, 204} and not response.body.strip():
            return {}
        return self._decode(response)

    @staticmethod
    def _decode(response: HttpResponse) -> object:
        if response.status < 200 or response.status >= 300:
            raise ExternalProbeError(f"HTTP_{response.status}")
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("invalid JSON response") from exc

    @staticmethod
    def _parse_order(raw: object) -> OrderSnapshot:
        if not isinstance(raw, Mapping):
            raise ProtocolError("order response must be an object")
        try:
            status_key = str(raw["status"]).lower()
            status = _STATUS[status_key]
            updated = datetime.fromisoformat(str(raw["updated_at"]).replace("Z", "+00:00"))
            order = OrderSnapshot(
                client_order_id=str(raw["client_order_id"]),
                broker_order_id=str(raw["id"]),
                symbol=str(raw["symbol"]).upper(),
                side=Side(str(raw["side"]).upper()),
                quantity=Decimal(str(raw["qty"])),
                limit_price=Decimal(str(raw["limit_price"])),
                status=status,
                filled_quantity=Decimal(str(raw.get("filled_qty", "0"))),
                updated_at=updated,
            )
        except (KeyError, ValueError, ArithmeticError) as exc:
            raise ProtocolError("invalid order response") from exc
        order.validate()
        return order


class WebSocketConnection(Protocol):
    def send(self, message: str | bytes) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...
    def close(self) -> None: ...


class WebSocketConnector(Protocol):
    def __call__(self, url: str, *, timeout_seconds: float) -> WebSocketConnection: ...


class WebsocketsConnector:
    def __call__(self, url: str, *, timeout_seconds: float) -> WebSocketConnection:
        if url != PAPER_STREAM:
            raise ProtocolError("WebSocket is restricted to Alpaca paper stream")
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise DependencyUnavailable("install the marketdata extra for the external stream probe") from exc
        return connect(
            url, open_timeout=timeout_seconds, close_timeout=timeout_seconds,
            ping_interval=20, ping_timeout=timeout_seconds, max_size=1_048_576,
            max_queue=16,
        )


class ReadOnlyExternalProbe:
    def __init__(
        self, *, credentials: Credentials, gateway: AlpacaPaperGateway,
        connector: WebSocketConnector, generation: int, timeout_seconds: float = 10.0,
    ) -> None:
        if generation <= 0 or timeout_seconds <= 0:
            raise ValueError("probe generation/timeout invalid")
        if gateway.writes_enabled:
            raise ConfigurationError("read-only probe requires writes disabled")
        self.credentials = credentials
        self.gateway = gateway
        self.connector = connector
        self.generation = generation
        self.timeout_seconds = timeout_seconds

    def run(self, *, now: datetime | None = None) -> tuple[AccountSnapshot, Sequence[OrderSnapshot], StreamEvidence]:
        now = datetime.now(UTC) if now is None else require_aware(now, field="now")
        account = self.gateway.get_account()
        orders = self.gateway.list_open_orders()
        connection = self.connector(PAPER_STREAM, timeout_seconds=self.timeout_seconds)
        try:
            auth = json.dumps({"action": "auth", "key": self.credentials.key_id, "secret": self.credentials.secret_key})
            connection.send(auth)
            auth_reply = self._decode_frame(connection.recv(timeout=self.timeout_seconds))
            connection.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))
            listen_reply = self._decode_frame(connection.recv(timeout=self.timeout_seconds))
        except (TimeoutError, OSError) as exc:
            raise ExternalProbeError(f"stream probe failed: {type(exc).__name__}") from exc
        finally:
            connection.close()
        authenticated = self._contains_status(auth_reply, "authorized")
        listening = self._contains_stream(listen_reply, "trade_updates")
        evidence = StreamEvidence(
            captured_at=now,
            generation=self.generation,
            authenticated=authenticated,
            listening=listening,
            credential_fingerprint=self.credentials.fingerprint,
            rest_endpoint=PAPER_REST,
            stream_endpoint=PAPER_STREAM,
            reasons=() if authenticated and listening else ("STREAM_HANDSHAKE_INCOMPLETE",),
        )
        evidence.validate()
        return account, orders, evidence

    @staticmethod
    def _decode_frame(frame: str | bytes) -> object:
        try:
            text = frame.decode("utf-8") if isinstance(frame, bytes) else frame
            return json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("invalid stream JSON") from exc

    @staticmethod
    def _contains_status(payload: object, expected: str) -> bool:
        documents = payload if isinstance(payload, list) else [payload]
        return any(isinstance(item, Mapping) and str(item.get("data", {}).get("status", "")).lower() == expected for item in documents)

    @staticmethod
    def _contains_stream(payload: object, expected: str) -> bool:
        documents = payload if isinstance(payload, list) else [payload]
        for item in documents:
            if not isinstance(item, Mapping):
                continue
            streams = item.get("data", {}).get("streams", [])
            if isinstance(streams, list) and expected in streams:
                return True
        return False
