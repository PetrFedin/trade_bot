from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import hmac
import json
import os
from pathlib import Path
import threading
import time
from typing import Protocol

UTC = timezone.utc
ZERO_DIGEST = "0" * 64


def require_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def canonical_json(value: object) -> str:
    def default(item: object) -> object:
        if isinstance(item, datetime):
            return require_aware(item, field="datetime").isoformat()
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return asdict(item)
        raise TypeError(type(item).__name__)

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=default)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class QualificationError(RuntimeError):
    pass


class QualificationBlocked(QualificationError):
    pass


class QualificationCorruption(QualificationError):
    pass


class StaleGeneration(QualificationError):
    pass


class ApprovalReplay(QualificationError):
    pass


class AmbiguousMutation(QualificationError):
    """Raised by a gateway when the broker may have accepted a mutation."""


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REPLACED = "REPLACED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"


class State(str, Enum):
    CREATED = "CREATED"
    PROBING = "PROBING"
    PROBE_VERIFIED = "PROBE_VERIFIED"
    ARMED = "ARMED"
    MUTATING = "MUTATING"
    CLEANUP = "CLEANUP"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    RECOVERING = "RECOVERING"
    QUARANTINED = "QUARANTINED"


class EventType(str, Enum):
    PROBE_STARTED = "PROBE_STARTED"
    PROBE_VERIFIED = "PROBE_VERIFIED"
    ARMED = "ARMED"
    ROUND_TRIP_STARTED = "ROUND_TRIP_STARTED"
    SUBMIT_CONFIRMED = "SUBMIT_CONFIRMED"
    REPLACE_CONFIRMED = "REPLACE_CONFIRMED"
    CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
    CLEANUP_STARTED = "CLEANUP_STARTED"
    CLEANUP_VERIFIED = "CLEANUP_VERIFIED"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    QUARANTINED = "QUARANTINED"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"


TRANSITIONS: Mapping[tuple[State, EventType], set[State]] = {
    (State.CREATED, EventType.PROBE_STARTED): {State.PROBING},
    (State.PROBING, EventType.PROBE_VERIFIED): {State.PROBE_VERIFIED},
    (State.PROBING, EventType.BLOCKED): {State.BLOCKED},
    (State.PROBING, EventType.RECOVERY_REQUIRED): {State.RECOVERING},
    (State.PROBING, EventType.QUARANTINED): {State.QUARANTINED},
    (State.PROBE_VERIFIED, EventType.ARMED): {State.ARMED},
    (State.PROBE_VERIFIED, EventType.BLOCKED): {State.BLOCKED},
    (State.ARMED, EventType.ROUND_TRIP_STARTED): {State.MUTATING},
    (State.ARMED, EventType.BLOCKED): {State.BLOCKED},
    (State.MUTATING, EventType.SUBMIT_CONFIRMED): {State.MUTATING},
    (State.MUTATING, EventType.REPLACE_CONFIRMED): {State.MUTATING},
    (State.MUTATING, EventType.CANCEL_CONFIRMED): {State.MUTATING},
    (State.MUTATING, EventType.CLEANUP_STARTED): {State.CLEANUP},
    (State.MUTATING, EventType.BLOCKED): {State.BLOCKED},
    (State.MUTATING, EventType.RECOVERY_REQUIRED): {State.RECOVERING},
    (State.MUTATING, EventType.QUARANTINED): {State.QUARANTINED},
    (State.CLEANUP, EventType.CLEANUP_VERIFIED): {State.CLEANUP},
    (State.CLEANUP, EventType.VERIFIED): {State.VERIFIED},
    (State.CLEANUP, EventType.BLOCKED): {State.BLOCKED},
    (State.CLEANUP, EventType.RECOVERY_REQUIRED): {State.RECOVERING},
    (State.CLEANUP, EventType.QUARANTINED): {State.QUARANTINED},
    (State.RECOVERING, EventType.BLOCKED): {State.BLOCKED},
    (State.RECOVERING, EventType.QUARANTINED): {State.QUARANTINED},
}


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    status: str
    currency: str
    buying_power: Decimal
    trading_blocked: bool = False

    def validate(self) -> None:
        if not self.account_id.strip():
            raise ValueError("account_id is required")
        if not self.status.strip() or not self.currency.strip():
            raise ValueError("account status and currency are required")
        if self.buying_power < 0 or not self.buying_power.is_finite():
            raise ValueError("buying_power must be non-negative and finite")


@dataclass(frozen=True)
class OrderSnapshot:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    limit_price: Decimal
    status: OrderStatus
    filled_quantity: Decimal
    updated_at: datetime

    def validate(self) -> None:
        require_aware(self.updated_at, field="updated_at")
        if not self.client_order_id.strip() or not self.broker_order_id.strip():
            raise ValueError("order identifiers are required")
        if self.symbol != self.symbol.strip().upper() or not self.symbol:
            raise ValueError("symbol must be normalized uppercase")
        if self.quantity <= 0 or not self.quantity.is_finite():
            raise ValueError("quantity must be positive and finite")
        if self.limit_price <= 0 or not self.limit_price.is_finite():
            raise ValueError("limit_price must be positive and finite")
        if self.filled_quantity < 0 or self.filled_quantity > self.quantity:
            raise ValueError("filled_quantity outside order quantity")


class PaperGateway(Protocol):
    paper_only: bool
    writes_enabled: bool
    credential_fingerprint: str
    rest_endpoint: str
    stream_endpoint: str

    def get_account(self) -> AccountSnapshot: ...
    def list_open_orders(self) -> Sequence[OrderSnapshot]: ...
    def submit_limit_order(
        self, *, client_order_id: str, symbol: str, side: Side,
        quantity: Decimal, limit_price: Decimal,
    ) -> OrderSnapshot: ...
    def replace_limit_order(
        self, *, broker_order_id: str, limit_price: Decimal,
    ) -> OrderSnapshot: ...
    def cancel_order(self, *, broker_order_id: str) -> OrderSnapshot: ...
    def get_order_by_client_order_id(self, client_order_id: str) -> OrderSnapshot | None: ...


@dataclass(frozen=True)
class Policy:
    maximum_plan_age: timedelta = timedelta(minutes=5)
    maximum_probe_age: timedelta = timedelta(seconds=30)
    maximum_approval_age: timedelta = timedelta(minutes=5)
    maximum_quantity: Decimal = Decimal("1")
    maximum_notional: Decimal = Decimal("100")
    maximum_open_orders: int = 0
    cleanup_attempts: int = 3
    cleanup_backoff_seconds: float = 0.01
    required_account_status: str = "ACTIVE"
    required_currency: str = "USD"
    allowed_symbols: frozenset[str] = frozenset()

    def validate(self) -> None:
        if min(self.maximum_plan_age, self.maximum_probe_age, self.maximum_approval_age) <= timedelta(0):
            raise ValueError("policy durations must be positive")
        if self.maximum_quantity <= 0 or not self.maximum_quantity.is_finite():
            raise ValueError("maximum_quantity must be positive and finite")
        if self.maximum_notional <= 0 or not self.maximum_notional.is_finite():
            raise ValueError("maximum_notional must be positive and finite")
        if self.maximum_open_orders < 0 or self.cleanup_attempts < 1:
            raise ValueError("policy counts are invalid")
        if self.cleanup_backoff_seconds < 0:
            raise ValueError("cleanup_backoff_seconds cannot be negative")
        normalized = frozenset(item.strip().upper() for item in self.allowed_symbols)
        if normalized != self.allowed_symbols or "" in normalized:
            raise ValueError("allowed_symbols must be uppercase and non-empty")


class ApprovalKey:
    ENV = "ASTRA_SANDBOX_APPROVAL_HMAC_KEY"
    __slots__ = ("_secret", "fingerprint")

    def __init__(self, secret: str) -> None:
        if len(secret.strip()) < 32:
            raise ValueError("approval secret must contain at least 32 characters")
        self._secret = secret.strip().encode("utf-8")
        self.fingerprint = hashlib.sha256(self._secret).hexdigest()[:16]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "ApprovalKey":
        source = os.environ if environment is None else environment
        if cls.ENV not in source:
            raise ValueError(f"missing {cls.ENV}")
        return cls(source[cls.ENV])

    def sign(self, document: Mapping[str, object]) -> str:
        return hmac.new(self._secret, canonical_json(document).encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, document: Mapping[str, object], signature: str) -> bool:
        return hmac.compare_digest(self.sign(document), signature)

    def __repr__(self) -> str:
        return f"ApprovalKey(fingerprint={self.fingerprint!r}, redacted=True)"


@dataclass(frozen=True)
class Approval:
    approval_id: str
    operator_id: str
    nonce: str
    generation: int
    account_id: str
    symbol: str
    side: Side
    maximum_quantity: Decimal
    maximum_notional: Decimal
    issued_at: datetime
    expires_at: datetime
    allow_paper_mutations: bool
    key_fingerprint: str = ""
    signature: str = ""
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    def signing_document(self) -> Mapping[str, object]:
        return {
            "approval_id": self.approval_id,
            "operator_id": self.operator_id,
            "nonce": self.nonce,
            "generation": self.generation,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "maximum_quantity": str(self.maximum_quantity),
            "maximum_notional": str(self.maximum_notional),
            "issued_at": require_aware(self.issued_at, field="issued_at").isoformat(),
            "expires_at": require_aware(self.expires_at, field="expires_at").isoformat(),
            "allow_paper_mutations": self.allow_paper_mutations,
            "external_order_routing_allowed": False,
            "live_trading_allowed": False,
        }

    def validate(self) -> None:
        if not all((self.approval_id.strip(), self.operator_id.strip(), self.account_id.strip())):
            raise ValueError("approval identity is incomplete")
        if len(self.nonce) < 16 or self.generation <= 0:
            raise ValueError("approval nonce/generation is invalid")
        if self.symbol != self.symbol.strip().upper() or not self.symbol:
            raise ValueError("approval symbol must be uppercase")
        if self.maximum_quantity <= 0 or self.maximum_notional <= 0:
            raise ValueError("approval limits must be positive")
        if require_aware(self.expires_at, field="expires_at") <= require_aware(self.issued_at, field="issued_at"):
            raise ValueError("approval expiry must be after issue")
        if not self.allow_paper_mutations:
            raise ValueError("paper mutations must be explicitly approved")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise ValueError("live routing flags are forbidden")
        if self.signature and (len(self.signature) != 64 or len(self.key_fingerprint) != 16):
            raise ValueError("approval seal is invalid")

    def seal(self, key: ApprovalKey) -> "Approval":
        unsigned = replace(self, key_fingerprint="", signature="")
        unsigned.validate()
        return replace(unsigned, key_fingerprint=key.fingerprint, signature=key.sign(unsigned.signing_document()))

    def verify(self, key: ApprovalKey) -> bool:
        self.validate()
        return self.key_fingerprint == key.fingerprint and key.verify(self.signing_document(), self.signature)

    @property
    def nonce_digest(self) -> str:
        return hashlib.sha256(self.nonce.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Plan:
    qualification_id: str
    generation: int
    expected_account_id: str
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    initial_limit_price: Decimal
    replacement_limit_price: Decimal | None
    created_at: datetime
    expires_at: datetime
    approval_id: str
    require_replace: bool = True
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False
    plan_digest: str = ""

    def document(self) -> Mapping[str, object]:
        return {
            "qualification_id": self.qualification_id,
            "generation": self.generation,
            "expected_account_id": self.expected_account_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "initial_limit_price": str(self.initial_limit_price),
            "replacement_limit_price": None if self.replacement_limit_price is None else str(self.replacement_limit_price),
            "created_at": require_aware(self.created_at, field="created_at").isoformat(),
            "expires_at": require_aware(self.expires_at, field="expires_at").isoformat(),
            "approval_id": self.approval_id,
            "require_replace": self.require_replace,
            "external_order_routing_allowed": False,
            "live_trading_allowed": False,
        }

    def validate(self) -> None:
        if not all((self.qualification_id.strip(), self.expected_account_id.strip(), self.client_order_id.strip(), self.approval_id.strip())):
            raise ValueError("plan identity is incomplete")
        if self.generation <= 0 or self.symbol != self.symbol.strip().upper() or not self.symbol:
            raise ValueError("plan generation/symbol is invalid")
        if self.quantity <= 0 or self.initial_limit_price <= 0:
            raise ValueError("plan quantity/price must be positive")
        if require_aware(self.expires_at, field="expires_at") <= require_aware(self.created_at, field="created_at"):
            raise ValueError("plan expiry must be after creation")
        if self.require_replace:
            if self.replacement_limit_price is None or self.replacement_limit_price <= 0 or self.replacement_limit_price == self.initial_limit_price:
                raise ValueError("valid distinct replacement price is required")
        elif self.replacement_limit_price is not None:
            raise ValueError("replacement price must be omitted")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise ValueError("live routing flags are forbidden")
        if self.plan_digest and self.plan_digest != digest(self.document()):
            raise QualificationCorruption("plan digest mismatch")

    def seal(self) -> "Plan":
        unsigned = replace(self, plan_digest="")
        unsigned.validate()
        return replace(unsigned, plan_digest=digest(unsigned.document()))


@dataclass(frozen=True)
class StreamEvidence:
    captured_at: datetime
    generation: int
    authenticated: bool
    listening: bool
    credential_fingerprint: str
    rest_endpoint: str
    stream_endpoint: str
    reasons: tuple[str, ...] = ()
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    def validate(self) -> None:
        require_aware(self.captured_at, field="captured_at")
        if self.generation <= 0 or len(self.credential_fingerprint) != 16:
            raise ValueError("stream evidence identity invalid")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise ValueError("live routing flags are forbidden")


@dataclass(frozen=True)
class Event:
    sequence: int
    qualification_id: str
    event_type: EventType
    from_state: State
    to_state: State
    occurred_at: datetime
    generation: int
    attributes: Mapping[str, object]
    previous_digest: str
    event_digest: str

    def base_document(self) -> Mapping[str, object]:
        return {
            "sequence": self.sequence,
            "qualification_id": self.qualification_id,
            "event_type": self.event_type.value,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "occurred_at": require_aware(self.occurred_at, field="occurred_at").isoformat(),
            "generation": self.generation,
            "attributes": dict(self.attributes),
            "previous_digest": self.previous_digest,
        }

    def document(self) -> Mapping[str, object]:
        return {**self.base_document(), "event_digest": self.event_digest}


class EventStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def append(self, *, qualification_id: str, event_type: EventType, from_state: State,
               to_state: State, occurred_at: datetime, generation: int,
               attributes: Mapping[str, object]) -> Event:
        require_aware(occurred_at, field="occurred_at")
        allowed = TRANSITIONS.get((from_state, event_type), set())
        if event_type is EventType.KILL_SWITCH_ENGAGED:
            allowed = {from_state}
        if to_state not in allowed:
            raise QualificationError("invalid state transition")
        with self._lock:
            events = self.load()
            previous = events[-1] if events else None
            event = Event(
                sequence=1 if previous is None else previous.sequence + 1,
                qualification_id=qualification_id,
                event_type=event_type,
                from_state=from_state,
                to_state=to_state,
                occurred_at=occurred_at,
                generation=generation,
                attributes={**dict(attributes), "external_order_routing_allowed": False, "live_trading_allowed": False},
                previous_digest=ZERO_DIGEST if previous is None else previous.event_digest,
                event_digest="",
            )
            event = replace(event, event_digest=digest(event.base_document()))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(event.document()) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def load(self) -> tuple[Event, ...]:
        if not self.path.exists():
            return ()
        events: list[Event] = []
        previous_digest = ZERO_DIGEST
        previous_time: datetime | None = None
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                event = Event(
                    sequence=int(raw["sequence"]),
                    qualification_id=str(raw["qualification_id"]),
                    event_type=EventType(raw["event_type"]),
                    from_state=State(raw["from_state"]),
                    to_state=State(raw["to_state"]),
                    occurred_at=datetime.fromisoformat(str(raw["occurred_at"])),
                    generation=int(raw["generation"]),
                    attributes=dict(raw["attributes"]),
                    previous_digest=str(raw["previous_digest"]),
                    event_digest=str(raw["event_digest"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise QualificationCorruption(f"invalid event line {number}") from exc
            if event.sequence != len(events) + 1 or event.previous_digest != previous_digest:
                raise QualificationCorruption("event chain sequence/digest mismatch")
            if event.event_digest != digest(event.base_document()):
                raise QualificationCorruption("event digest mismatch")
            if previous_time is not None and event.occurred_at < previous_time:
                raise QualificationCorruption("event time regression")
            allowed = TRANSITIONS.get((event.from_state, event.event_type), set())
            if event.event_type is EventType.KILL_SWITCH_ENGAGED:
                allowed = {event.from_state}
            if event.to_state not in allowed:
                raise QualificationCorruption("persisted transition invalid")
            if event.attributes.get("external_order_routing_allowed") or event.attributes.get("live_trading_allowed"):
                raise QualificationCorruption("forbidden routing flag in journal")
            events.append(event)
            previous_digest = event.event_digest
            previous_time = event.occurred_at
        return tuple(events)

    def verify(self) -> bool:
        try:
            self.load()
            return True
        except QualificationCorruption:
            return False

    def approval_consumed(self, approval: Approval) -> bool:
        return any(
            event.event_type is EventType.ARMED
            and (event.attributes.get("approval_id") == approval.approval_id
                 or event.attributes.get("approval_nonce_digest") == approval.nonce_digest)
            for event in self.load()
        )


@dataclass(frozen=True)
class KillSwitchStatus:
    engaged: bool
    reason: str
    engaged_at: datetime | None
    generation: int
    status_digest: str


class KillSwitchStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def status(self) -> KillSwitchStatus:
        if not self.path.exists():
            return KillSwitchStatus(False, "", None, 0, ZERO_DIGEST)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            unsigned = {k: raw[k] for k in ("engaged", "reason", "engaged_at", "generation")}
            if str(raw["status_digest"]) != digest(unsigned):
                raise QualificationCorruption("kill switch digest mismatch")
            return KillSwitchStatus(
                bool(raw["engaged"]), str(raw["reason"]),
                None if raw["engaged_at"] is None else datetime.fromisoformat(str(raw["engaged_at"])),
                int(raw["generation"]), str(raw["status_digest"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, QualificationCorruption):
                raise
            raise QualificationCorruption("invalid kill switch") from exc

    def engage(self, *, reason: str, now: datetime, generation: int) -> KillSwitchStatus:
        require_aware(now, field="now")
        if not reason.strip() or generation <= 0:
            raise ValueError("kill switch reason/generation required")
        with self._lock:
            current = self.status()
            if current.engaged:
                return current
            unsigned = {"engaged": True, "reason": reason.strip(), "engaged_at": now.isoformat(), "generation": generation}
            atomic_write(self.path, canonical_json({**unsigned, "status_digest": digest(unsigned)}) + "\n")
            return self.status()


@dataclass(frozen=True)
class Result:
    qualification_id: str
    state: State
    success: bool
    reasons: tuple[str, ...]
    broker_order_id: str
    filled_quantity: Decimal
    journal_tail_digest: str
    kill_switch_engaged: bool
    read_only_probe_verified: bool
    paper_round_trip_verified: bool
    cleanup_verified: bool
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False


class QualificationService:
    PAPER_REST = "https://paper-api.alpaca.markets"
    PAPER_STREAM = "wss://paper-api.alpaca.markets/stream"

    def __init__(self, *, gateway: PaperGateway, plan: Plan, approval_key: ApprovalKey,
                 event_store: EventStore, kill_switch: KillSwitchStore,
                 policy: Policy = Policy(), sleeper: Callable[[float], None] = time.sleep) -> None:
        plan.validate()
        if not plan.plan_digest:
            raise ValueError("plan must be sealed")
        policy.validate()
        if not event_store.verify():
            raise QualificationCorruption("event store invalid")
        self.gateway = gateway
        self.plan = plan
        self.approval_key = approval_key
        self.event_store = event_store
        self.kill_switch = kill_switch
        self.policy = policy
        self.sleeper = sleeper
        self.events = tuple(event for event in event_store.load() if event.qualification_id == plan.qualification_id)
        self.state = self.events[-1].to_state if self.events else State.CREATED
        self.probe_captured_at: datetime | None = None
        self.broker_order_id = ""
        self.filled_quantity = Decimal("0")
        self._lock = threading.RLock()

    def probe(self, *, now: datetime, expected_generation: int, stream: StreamEvidence) -> Result:
        now = require_aware(now, field="now")
        self._check_generation(expected_generation)
        stream.validate()
        with self._lock:
            if self.state is not State.CREATED:
                raise QualificationError(f"probe cannot start from {self.state.value}")
            self._transition(EventType.PROBE_STARTED, State.PROBING, now, {"plan_digest": self.plan.plan_digest})
            try:
                account = self.gateway.get_account()
                orders = tuple(self.gateway.list_open_orders())
                account.validate()
                for order in orders:
                    order.validate()
            except Exception as exc:
                return self._recover(now, f"READ_ONLY_PROBE_FAILED:{type(exc).__name__}")
            reasons = self._preflight_reasons(account, orders, stream, now)
            security_reasons = {"NOT_PAPER_GATEWAY", "REST_ENDPOINT_NOT_PAPER", "STREAM_ENDPOINT_NOT_PAPER", "CREDENTIAL_FINGERPRINT_MISMATCH", "STREAM_GENERATION_MISMATCH"}
            if any(reason in security_reasons for reason in reasons):
                return self._quarantine(now, reasons)
            if reasons:
                return self._block(now, reasons)
            self.probe_captured_at = now
            self._transition(EventType.PROBE_VERIFIED, State.PROBE_VERIFIED, now, {
                "account_id": account.account_id,
                "account_status": account.status,
                "currency": account.currency,
                "open_order_count": len(orders),
                "stream_authenticated": stream.authenticated,
                "stream_listening": stream.listening,
                "credential_fingerprint": stream.credential_fingerprint,
            })
            return self._result(())

    def arm(self, *, approval: Approval, now: datetime, expected_generation: int) -> Result:
        now = require_aware(now, field="now")
        self._check_generation(expected_generation)
        with self._lock:
            if self.state is not State.PROBE_VERIFIED:
                raise QualificationError(f"arm cannot start from {self.state.value}")
            reasons: list[str] = []
            try:
                valid_signature = approval.verify(self.approval_key)
            except ValueError:
                valid_signature = False
            if not valid_signature:
                reasons.append("APPROVAL_SIGNATURE_INVALID")
            if approval.approval_id != self.plan.approval_id:
                reasons.append("APPROVAL_ID_MISMATCH")
            if approval.generation != self.plan.generation:
                reasons.append("APPROVAL_GENERATION_MISMATCH")
            if approval.account_id != self.plan.expected_account_id:
                reasons.append("APPROVAL_ACCOUNT_MISMATCH")
            if approval.symbol != self.plan.symbol or approval.side is not self.plan.side:
                reasons.append("APPROVAL_SCOPE_MISMATCH")
            if approval.maximum_quantity < self.plan.quantity:
                reasons.append("APPROVAL_QUANTITY_TOO_SMALL")
            if approval.maximum_notional < self.plan.quantity * self.plan.initial_limit_price:
                reasons.append("APPROVAL_NOTIONAL_TOO_SMALL")
            if now < approval.issued_at or now > approval.expires_at or now - approval.issued_at > self.policy.maximum_approval_age:
                reasons.append("APPROVAL_NOT_CURRENT")
            if self.probe_captured_at is None or now - self.probe_captured_at > self.policy.maximum_probe_age:
                reasons.append("PROBE_EVIDENCE_STALE")
            if not self.gateway.writes_enabled:
                reasons.append("PAPER_WRITES_DISABLED")
            if self.kill_switch.status().engaged:
                reasons.append("KILL_SWITCH_ENGAGED")
            if self.event_store.approval_consumed(approval):
                raise ApprovalReplay("approval id/nonce already consumed")
            if reasons:
                return self._block(now, tuple(sorted(set(reasons))))
            self._transition(EventType.ARMED, State.ARMED, now, {
                "approval_id": approval.approval_id,
                "operator_id": approval.operator_id,
                "approval_nonce_digest": approval.nonce_digest,
                "approval_key_fingerprint": approval.key_fingerprint,
                "approval_signature": approval.signature,
            })
            return self._result(())

    def execute(self, *, now: datetime, expected_generation: int) -> Result:
        now = require_aware(now, field="now")
        self._check_generation(expected_generation)
        with self._lock:
            if self.state is not State.ARMED:
                raise QualificationError(f"execute cannot start from {self.state.value}")
            if self.kill_switch.status().engaged:
                return self._block(now, ("KILL_SWITCH_ENGAGED",))
            try:
                account = self.gateway.get_account()
                orders = tuple(self.gateway.list_open_orders())
            except Exception as exc:
                return self._recover(now, f"PREFLIGHT_READ_FAILED:{type(exc).__name__}")
            preflight = self._account_order_reasons(account, orders)
            if preflight:
                return self._block(now, preflight)
            self._transition(EventType.ROUND_TRIP_STARTED, State.MUTATING, now, {
                "client_order_id": self.plan.client_order_id,
                "symbol": self.plan.symbol,
                "quantity": str(self.plan.quantity),
            })
            submitted = self._mutate("SUBMIT", now, lambda: self.gateway.submit_limit_order(
                client_order_id=self.plan.client_order_id, symbol=self.plan.symbol, side=self.plan.side,
                quantity=self.plan.quantity, limit_price=self.plan.initial_limit_price))
            if isinstance(submitted, Result):
                return submitted
            if (reasons := self._order_reasons(submitted, self.plan.initial_limit_price)):
                return self._quarantine(now, reasons)
            if submitted.filled_quantity > 0:
                return self._residual(now)
            self.broker_order_id = submitted.broker_order_id
            self._transition(EventType.SUBMIT_CONFIRMED, State.MUTATING, now, self._order_attributes(submitted))
            current = submitted
            if self.plan.require_replace:
                replaced = self._mutate("REPLACE", now, lambda: self.gateway.replace_limit_order(
                    broker_order_id=current.broker_order_id,
                    limit_price=self.plan.replacement_limit_price or self.plan.initial_limit_price))
                if isinstance(replaced, Result):
                    return replaced
                if (reasons := self._order_reasons(replaced, self.plan.replacement_limit_price or self.plan.initial_limit_price)):
                    return self._quarantine(now, reasons)
                if replaced.filled_quantity > 0:
                    return self._residual(now)
                current = replaced
                self._transition(EventType.REPLACE_CONFIRMED, State.MUTATING, now, self._order_attributes(current))
            cancelled = self._mutate("CANCEL", now, lambda: self.gateway.cancel_order(broker_order_id=current.broker_order_id))
            if isinstance(cancelled, Result):
                return cancelled
            if cancelled.filled_quantity > 0:
                return self._residual(now)
            self._transition(EventType.CANCEL_CONFIRMED, State.MUTATING, now, self._order_attributes(cancelled))
            self._transition(EventType.CLEANUP_STARTED, State.CLEANUP, now, {"broker_order_id": cancelled.broker_order_id})
            return self._cleanup(now)

    def recover_read_only(self, *, now: datetime, expected_generation: int) -> Result:
        now = require_aware(now, field="now")
        self._check_generation(expected_generation)
        if self.state is not State.RECOVERING:
            raise QualificationError("recovery is only valid from RECOVERING")
        try:
            order = self.gateway.get_order_by_client_order_id(self.plan.client_order_id)
            open_orders = tuple(self.gateway.list_open_orders())
        except Exception as exc:
            return self._result((f"RECOVERY_READ_FAILED:{type(exc).__name__}",))
        if order is not None and order.filled_quantity > 0:
            return self._residual(now)
        if any(item.client_order_id == self.plan.client_order_id for item in open_orders):
            self._engage_kill_switch(now, "UNRESOLVED_OPEN_ORDER")
            return self._result(("UNRESOLVED_OPEN_ORDER",))
        return self._block(now, ("ROUND_TRIP_NOT_PROVEN_AFTER_RECOVERY",))

    def _mutate(self, name: str, now: datetime, operation: Callable[[], OrderSnapshot]) -> OrderSnapshot | Result:
        try:
            return operation()
        except AmbiguousMutation:
            try:
                recovered = self.gateway.get_order_by_client_order_id(self.plan.client_order_id)
            except Exception as exc:
                return self._recover(now, f"{name}_AMBIGUOUS_LOOKUP_FAILED:{type(exc).__name__}")
            if recovered is None:
                return self._recover(now, f"{name}_AMBIGUOUS_NOT_FOUND")
            if name == "CANCEL" and recovered.status not in {OrderStatus.CANCELLED, OrderStatus.REJECTED}:
                return self._recover(now, "CANCEL_AMBIGUOUS_ORDER_ACTIVE")
            return recovered
        except Exception as exc:
            return self._recover(now, f"{name}_FAILED:{type(exc).__name__}")

    def _cleanup(self, now: datetime) -> Result:
        reason = "CLEANUP_NOT_PROVEN"
        for attempt in range(1, self.policy.cleanup_attempts + 1):
            try:
                open_orders = tuple(self.gateway.list_open_orders())
                order = self.gateway.get_order_by_client_order_id(self.plan.client_order_id)
            except Exception as exc:
                reason = f"CLEANUP_READ_FAILED:{type(exc).__name__}"
            else:
                if order is not None and order.filled_quantity > 0:
                    return self._residual(now)
                still_open = any(item.client_order_id == self.plan.client_order_id for item in open_orders)
                terminal = order is None or order.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED}
                if not still_open and terminal:
                    self._transition(EventType.CLEANUP_VERIFIED, State.CLEANUP, now, {"attempt": attempt})
                    self._transition(EventType.VERIFIED, State.VERIFIED, now, {
                        "read_only_probe_verified": True,
                        "paper_round_trip_verified": True,
                        "cleanup_verified": True,
                    })
                    return self._result(())
                reason = "ORDER_REMAINS_OPEN_OR_NON_TERMINAL"
            if attempt < self.policy.cleanup_attempts:
                self.sleeper(self.policy.cleanup_backoff_seconds)
        self._engage_kill_switch(now, reason)
        return self._block(now, (reason,))

    def _preflight_reasons(self, account: AccountSnapshot, orders: Sequence[OrderSnapshot], stream: StreamEvidence, now: datetime) -> tuple[str, ...]:
        reasons = list(self._account_order_reasons(account, orders))
        if not self.gateway.paper_only:
            reasons.append("NOT_PAPER_GATEWAY")
        if self.gateway.rest_endpoint != self.PAPER_REST or stream.rest_endpoint != self.PAPER_REST:
            reasons.append("REST_ENDPOINT_NOT_PAPER")
        if self.gateway.stream_endpoint != self.PAPER_STREAM or stream.stream_endpoint != self.PAPER_STREAM:
            reasons.append("STREAM_ENDPOINT_NOT_PAPER")
        if stream.credential_fingerprint != self.gateway.credential_fingerprint:
            reasons.append("CREDENTIAL_FINGERPRINT_MISMATCH")
        if stream.generation != self.plan.generation:
            reasons.append("STREAM_GENERATION_MISMATCH")
        if not stream.authenticated or not stream.listening or stream.reasons:
            reasons.append("STREAM_NOT_READY")
        if now < self.plan.created_at or now > self.plan.expires_at or now - self.plan.created_at > self.policy.maximum_plan_age:
            reasons.append("PLAN_NOT_CURRENT")
        if self.kill_switch.status().engaged:
            reasons.append("KILL_SWITCH_ENGAGED")
        return tuple(sorted(set(reasons)))

    def _account_order_reasons(self, account: AccountSnapshot, orders: Sequence[OrderSnapshot]) -> tuple[str, ...]:
        reasons: list[str] = []
        if account.account_id != self.plan.expected_account_id:
            reasons.append("ACCOUNT_ID_MISMATCH")
        if account.status.upper() != self.policy.required_account_status.upper():
            reasons.append("ACCOUNT_NOT_ACTIVE")
        if account.currency.upper() != self.policy.required_currency.upper():
            reasons.append("ACCOUNT_CURRENCY_MISMATCH")
        if account.trading_blocked or account.buying_power <= 0:
            reasons.append("ACCOUNT_NOT_TRADABLE")
        if len(orders) > self.policy.maximum_open_orders:
            reasons.append("OPEN_ORDER_BASELINE_NOT_EMPTY")
        if any(order.client_order_id == self.plan.client_order_id for order in orders):
            reasons.append("DUPLICATE_CLIENT_ORDER_ID")
        if self.plan.quantity > self.policy.maximum_quantity:
            reasons.append("QUANTITY_LIMIT_EXCEEDED")
        if self.plan.quantity * self.plan.initial_limit_price > self.policy.maximum_notional:
            reasons.append("NOTIONAL_LIMIT_EXCEEDED")
        if self.policy.allowed_symbols and self.plan.symbol not in self.policy.allowed_symbols:
            reasons.append("SYMBOL_NOT_ALLOWLISTED")
        return tuple(sorted(set(reasons)))

    def _order_reasons(self, order: OrderSnapshot, expected_price: Decimal) -> tuple[str, ...]:
        order.validate()
        if (order.client_order_id != self.plan.client_order_id or order.symbol != self.plan.symbol
                or order.side is not self.plan.side or order.quantity != self.plan.quantity
                or order.limit_price != expected_price):
            return ("ORDER_IDENTITY_MISMATCH",)
        return ()

    @staticmethod
    def _order_attributes(order: OrderSnapshot) -> Mapping[str, object]:
        return {
            "client_order_id": order.client_order_id,
            "broker_order_id": order.broker_order_id,
            "symbol": order.symbol,
            "status": order.status.value,
            "quantity": str(order.quantity),
            "limit_price": str(order.limit_price),
            "filled_quantity": str(order.filled_quantity),
            "broker_updated_at": order.updated_at.isoformat(),
        }

    def _transition(self, event_type: EventType, to_state: State, now: datetime, attributes: Mapping[str, object]) -> None:
        event = self.event_store.append(
            qualification_id=self.plan.qualification_id, event_type=event_type,
            from_state=self.state, to_state=to_state, occurred_at=now,
            generation=self.plan.generation, attributes=attributes)
        self.state = to_state
        self.events = (*self.events, event)

    def _engage_kill_switch(self, now: datetime, reason: str) -> None:
        status = self.kill_switch.engage(reason=reason, now=now, generation=self.plan.generation)
        event = self.event_store.append(
            qualification_id=self.plan.qualification_id, event_type=EventType.KILL_SWITCH_ENGAGED,
            from_state=self.state, to_state=self.state, occurred_at=now,
            generation=self.plan.generation, attributes={"reason": status.reason, "status_digest": status.status_digest})
        self.events = (*self.events, event)

    def _recover(self, now: datetime, reason: str) -> Result:
        self._engage_kill_switch(now, reason)
        self._transition(EventType.RECOVERY_REQUIRED, State.RECOVERING, now, {"reason": reason})
        return self._result((reason,))

    def _block(self, now: datetime, reasons: tuple[str, ...]) -> Result:
        if self.state not in {State.BLOCKED, State.QUARANTINED, State.VERIFIED}:
            self._transition(EventType.BLOCKED, State.BLOCKED, now, {"reasons": reasons})
        return self._result(reasons)

    def _quarantine(self, now: datetime, reasons: tuple[str, ...]) -> Result:
        self._engage_kill_switch(now, ";".join(reasons))
        self._transition(EventType.QUARANTINED, State.QUARANTINED, now, {"reasons": reasons})
        return self._result(reasons)

    def _residual(self, now: datetime) -> Result:
        self.filled_quantity = max(self.filled_quantity, Decimal("0.00000001"))
        self._engage_kill_switch(now, "RESIDUAL_PAPER_EXPOSURE")
        return self._block(now, ("RESIDUAL_PAPER_EXPOSURE",))

    def _check_generation(self, expected: int) -> None:
        if expected != self.plan.generation:
            raise StaleGeneration(f"expected {expected}, active {self.plan.generation}")

    def _result(self, reasons: tuple[str, ...]) -> Result:
        tail = self.events[-1].event_digest if self.events else ZERO_DIGEST
        return Result(
            qualification_id=self.plan.qualification_id,
            state=self.state,
            success=self.state is State.VERIFIED,
            reasons=reasons,
            broker_order_id=self.broker_order_id,
            filled_quantity=self.filled_quantity,
            journal_tail_digest=tail,
            kill_switch_engaged=self.kill_switch.status().engaged,
            read_only_probe_verified=self.state in {State.PROBE_VERIFIED, State.ARMED, State.MUTATING, State.CLEANUP, State.VERIFIED},
            paper_round_trip_verified=self.state is State.VERIFIED,
            cleanup_verified=self.state is State.VERIFIED,
        )
