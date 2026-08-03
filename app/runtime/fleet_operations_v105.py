from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import json
import math
import re
import threading
from typing import Any, Iterable, Mapping

UTC = timezone.utc
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")


class FleetErrorV105(RuntimeError):
    """Base class for fail-closed fleet operations errors."""


class PolicyErrorV105(FleetErrorV105):
    pass


class SignatureErrorV105(FleetErrorV105):
    pass


class EnrollmentErrorV105(FleetErrorV105):
    pass


class ReplayErrorV105(EnrollmentErrorV105):
    pass


class WorkerStateErrorV105(FleetErrorV105):
    pass


class ContainmentErrorV105(FleetErrorV105):
    pass


class KeyStatusV105(str, Enum):
    ACTIVE = "ACTIVE"
    RETIRING = "RETIRING"
    REVOKED = "REVOKED"


class WorkerStateV105(str, Enum):
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    QUARANTINED = "QUARANTINED"
    REVOKED = "REVOKED"


class ContainmentScopeV105(str, Enum):
    FLEET = "FLEET"
    ZONE = "ZONE"
    DEPLOYMENT = "DEPLOYMENT"
    WORKER = "WORKER"


class ScaleActionV105(str, Enum):
    HOLD = "HOLD"
    SCALE_UP = "SCALE_UP"
    SCALE_DOWN = "SCALE_DOWN"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PolicyErrorV105(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical(value: Any) -> bytes:
    def normalize(item: Any) -> Any:
        if isinstance(item, datetime):
            return _aware(item, "datetime").isoformat().replace("+00:00", "Z")
        if isinstance(item, timedelta):
            return item.total_seconds()
        if isinstance(item, Enum):
            return item.value
        if hasattr(item, "__dataclass_fields__"):
            return normalize(asdict(item))
        if isinstance(item, Mapping):
            return {str(key): normalize(item[key]) for key in sorted(item)}
        if isinstance(item, (list, tuple)):
            return [normalize(entry) for entry in item]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        raise TypeError(f"unsupported canonical value: {type(item)!r}")

    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PolicyErrorV105(f"invalid {name}")
    return value


def _validate_hex64(value: str, name: str) -> str:
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise PolicyErrorV105(f"invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class FleetPolicyV105:
    fleet_id: str
    generation: int
    min_replicas: int
    max_replicas: int
    max_scale_up_step: int
    max_scale_down_step: int
    target_queue_per_worker: int
    heartbeat_ttl: timedelta
    enrollment_ttl: timedelta
    drain_timeout: timedelta
    scale_up_cooldown: timedelta
    scale_down_cooldown: timedelta
    stabilization_samples: int
    crash_budget: int
    dlq_budget: int
    allowed_clusters: tuple[str, ...]
    allowed_namespaces: tuple[str, ...]
    allowed_service_accounts: tuple[str, ...]
    allowed_zones: tuple[str, ...]
    allowed_s3_hosts: tuple[str, ...]
    evidence_bucket: str
    evidence_prefix: str

    def __post_init__(self) -> None:
        _validate_id(self.fleet_id, "fleet_id")
        if self.generation <= 0:
            raise PolicyErrorV105("generation must be positive")
        if self.min_replicas < 1 or self.max_replicas < self.min_replicas:
            raise PolicyErrorV105("invalid replica bounds")
        for name in ("max_scale_up_step", "max_scale_down_step", "target_queue_per_worker", "stabilization_samples"):
            if getattr(self, name) <= 0:
                raise PolicyErrorV105(f"{name} must be positive")
        if self.crash_budget < 0 or self.dlq_budget < 0:
            raise PolicyErrorV105("budgets cannot be negative")
        for name in ("heartbeat_ttl", "enrollment_ttl", "drain_timeout", "scale_up_cooldown", "scale_down_cooldown"):
            if getattr(self, name) <= timedelta(0):
                raise PolicyErrorV105(f"{name} must be positive")
        for name in ("allowed_clusters", "allowed_namespaces", "allowed_service_accounts", "allowed_zones", "allowed_s3_hosts"):
            values = getattr(self, name)
            if not values or any(not value.strip() for value in values) or len(set(values)) != len(values):
                raise PolicyErrorV105(f"invalid {name}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.evidence_bucket):
            raise PolicyErrorV105("invalid evidence_bucket")
        if self.evidence_prefix.startswith("/") or ".." in self.evidence_prefix.split("/"):
            raise PolicyErrorV105("invalid evidence_prefix")

    @property
    def digest(self) -> str:
        return _digest(self)

    @property
    def external_order_routing_allowed(self) -> bool:
        return False

    @property
    def live_trading_allowed(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class SigningKeyV105:
    key_id: str
    secret: bytes = field(repr=False)
    status: KeyStatusV105 = KeyStatusV105.ACTIVE
    not_before: datetime = field(default_factory=lambda: datetime.now(UTC))
    not_after: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(days=90))

    def __post_init__(self) -> None:
        _validate_id(self.key_id, "key_id")
        if len(self.secret) < 32:
            raise PolicyErrorV105("signing secret must be at least 32 bytes")
        if _aware(self.not_after, "not_after") <= _aware(self.not_before, "not_before"):
            raise PolicyErrorV105("invalid key validity window")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.secret).hexdigest()[:16]


class RotatingKeyRingV105:
    def __init__(self, keys: Iterable[SigningKeyV105] = ()) -> None:
        self._lock = threading.RLock()
        self._keys: dict[str, SigningKeyV105] = {}
        for key in keys:
            self.add(key)

    def add(self, key: SigningKeyV105) -> None:
        with self._lock:
            if key.key_id in self._keys:
                raise PolicyErrorV105("duplicate signing key")
            self._keys[key.key_id] = key

    def rotate(self, new_key: SigningKeyV105) -> None:
        if new_key.status is not KeyStatusV105.ACTIVE:
            raise PolicyErrorV105("new rotation key must be active")
        with self._lock:
            if new_key.key_id in self._keys:
                raise PolicyErrorV105("duplicate signing key")
            for key_id, key in tuple(self._keys.items()):
                if key.status is KeyStatusV105.ACTIVE:
                    self._keys[key_id] = replace(key, status=KeyStatusV105.RETIRING)
            self._keys[new_key.key_id] = new_key

    def revoke(self, key_id: str) -> None:
        with self._lock:
            key = self._keys.get(key_id)
            if key is None:
                raise PolicyErrorV105("unknown signing key")
            self._keys[key_id] = replace(key, status=KeyStatusV105.REVOKED)

    def sign(self, payload: bytes, now: datetime) -> tuple[str, str]:
        now = _aware(now, "now")
        with self._lock:
            candidates = [key for key in self._keys.values() if key.status is KeyStatusV105.ACTIVE and key.not_before <= now < key.not_after]
            if len(candidates) != 1:
                raise SignatureErrorV105("exactly one active signing key required")
            key = candidates[0]
            return key.key_id, hmac.new(key.secret, payload, hashlib.sha256).hexdigest()

    def verify(self, key_id: str, payload: bytes, signature: str, now: datetime) -> None:
        now = _aware(now, "now")
        with self._lock:
            key = self._keys.get(key_id)
            if key is None or key.status is KeyStatusV105.REVOKED:
                raise SignatureErrorV105("signing key unavailable")
            if not key.not_before <= now < key.not_after:
                raise SignatureErrorV105("signing key outside validity window")
            expected = hmac.new(key.secret, payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise SignatureErrorV105("invalid signature")

    def active_key_id(self, now: datetime) -> str:
        now = _aware(now, "now")
        with self._lock:
            candidates = [key.key_id for key in self._keys.values() if key.status is KeyStatusV105.ACTIVE and key.not_before <= now < key.not_after]
            if len(candidates) != 1:
                raise SignatureErrorV105("exactly one active signing key required")
            return candidates[0]

    def public_snapshot(self) -> tuple[dict[str, str], ...]:
        with self._lock:
            return tuple(
                {"key_id": key.key_id, "status": key.status.value, "fingerprint": key.fingerprint}
                for key in sorted(self._keys.values(), key=lambda item: item.key_id)
            )


@dataclass(frozen=True, slots=True)
class KubernetesAttestationV105:
    cluster: str
    namespace: str
    service_account: str
    pod_uid: str
    node_uid: str
    deployment_id: str
    zone: str
    image_digest: str
    config_digest: str
    audience: str

    def __post_init__(self) -> None:
        for name in ("cluster", "namespace", "service_account", "pod_uid", "node_uid", "deployment_id", "zone", "audience"):
            _validate_id(getattr(self, name), name)
        if not _SHA256_RE.fullmatch(self.image_digest):
            raise PolicyErrorV105("invalid image_digest")
        _validate_hex64(self.config_digest, "config_digest")


@dataclass(frozen=True, slots=True)
class SignedEnrollmentV105:
    token_id: str
    worker_id: str
    generation: int
    attestation_digest: str
    certificate_fingerprint: str
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    nonce: str
    key_id: str
    signature: str

    def __post_init__(self) -> None:
        for name in ("token_id", "worker_id", "nonce", "key_id"):
            _validate_id(getattr(self, name), name)
        if self.generation <= 0:
            raise PolicyErrorV105("generation must be positive")
        _validate_hex64(self.attestation_digest, "attestation_digest")
        _validate_hex64(self.certificate_fingerprint, "certificate_fingerprint")
        if not _HEX64_RE.fullmatch(self.signature):
            raise PolicyErrorV105("invalid signature encoding")
        issued = _aware(self.issued_at, "issued_at")
        not_before = _aware(self.not_before, "not_before")
        expires = _aware(self.expires_at, "expires_at")
        if not issued <= not_before < expires:
            raise PolicyErrorV105("invalid enrollment time window")

    def unsigned_payload(self) -> bytes:
        return _canonical({
            "token_id": self.token_id,
            "worker_id": self.worker_id,
            "generation": self.generation,
            "attestation_digest": self.attestation_digest,
            "certificate_fingerprint": self.certificate_fingerprint,
            "issued_at": self.issued_at,
            "not_before": self.not_before,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "key_id": self.key_id,
        })


@dataclass(frozen=True, slots=True)
class WorkerIdentityV105:
    worker_id: str
    deployment_id: str
    cluster: str
    namespace: str
    service_account: str
    zone: str
    certificate_fingerprint: str
    identity_generation: int
    state: WorkerStateV105
    enrolled_at: datetime
    last_heartbeat_at: datetime
    heartbeat_sequence: int = 0
    active_claims: int = 0
    drain_deadline: datetime | None = None
    recovery_required: bool = False


@dataclass(frozen=True, slots=True)
class FleetEventV105:
    sequence: int
    event_type: str
    worker_id: str | None
    occurred_at: datetime
    details: Mapping[str, Any]
    previous_digest: str
    digest: str


class FleetEventJournalV105:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: list[FleetEventV105] = []

    def append(self, event_type: str, occurred_at: datetime, *, worker_id: str | None = None, details: Mapping[str, Any] | None = None) -> FleetEventV105:
        occurred_at = _aware(occurred_at, "occurred_at")
        with self._lock:
            previous = self._events[-1].digest if self._events else "0" * 64
            sequence = len(self._events) + 1
            body = {
                "sequence": sequence,
                "event_type": event_type,
                "worker_id": worker_id,
                "occurred_at": occurred_at,
                "details": dict(details or {}),
                "previous_digest": previous,
            }
            event = FleetEventV105(sequence, event_type, worker_id, occurred_at, body["details"], previous, _digest(body))
            self._events.append(event)
            return event

    def verify(self) -> None:
        with self._lock:
            previous = "0" * 64
            for expected_sequence, event in enumerate(self._events, 1):
                if event.sequence != expected_sequence or event.previous_digest != previous:
                    raise FleetErrorV105("event chain sequence mismatch")
                body = {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "worker_id": event.worker_id,
                    "occurred_at": event.occurred_at,
                    "details": dict(event.details),
                    "previous_digest": event.previous_digest,
                }
                if not hmac.compare_digest(event.digest, _digest(body)):
                    raise FleetErrorV105("event chain digest mismatch")
                previous = event.digest

    @property
    def tail_digest(self) -> str:
        with self._lock:
            return self._events[-1].digest if self._events else "0" * 64

    @property
    def events(self) -> tuple[FleetEventV105, ...]:
        with self._lock:
            return tuple(self._events)


class FleetEnrollmentAuthorityV105:
    def __init__(self, policy: FleetPolicyV105, keyring: RotatingKeyRingV105) -> None:
        self.policy = policy
        self.keyring = keyring

    def issue(
        self,
        *,
        token_id: str,
        worker_id: str,
        attestation: KubernetesAttestationV105,
        certificate_fingerprint: str,
        nonce: str,
        now: datetime,
        not_before: datetime | None = None,
    ) -> SignedEnrollmentV105:
        now = _aware(now, "now")
        not_before = _aware(not_before or now, "not_before")
        _validate_hex64(certificate_fingerprint, "certificate_fingerprint")
        unsigned = {
            "token_id": _validate_id(token_id, "token_id"),
            "worker_id": _validate_id(worker_id, "worker_id"),
            "generation": self.policy.generation,
            "attestation_digest": _digest(attestation),
            "certificate_fingerprint": certificate_fingerprint,
            "issued_at": now,
            "not_before": not_before,
            "expires_at": now + self.policy.enrollment_ttl,
            "nonce": _validate_id(nonce, "nonce"),
        }
        key_id = self.keyring.active_key_id(now)
        payload = _canonical({**unsigned, "key_id": key_id})
        signed_key_id, signature = self.keyring.sign(payload, now)
        if signed_key_id != key_id:
            raise SignatureErrorV105("active key changed during issuance")
        return SignedEnrollmentV105(**unsigned, key_id=key_id, signature=signature)


@dataclass(frozen=True, slots=True)
class ContainmentRecordV105:
    containment_id: str
    scope: ContainmentScopeV105
    target: str
    epoch: int
    reason: str
    activated_at: datetime
    active: bool = True
    released_at: datetime | None = None
    release_evidence_digest: str | None = None
    released_by: tuple[str, str] | None = None


class FleetRegistryV105:
    def __init__(self, policy: FleetPolicyV105, keyring: RotatingKeyRingV105, journal: FleetEventJournalV105 | None = None) -> None:
        self.policy = policy
        self.keyring = keyring
        self.journal = journal or FleetEventJournalV105()
        self._lock = threading.RLock()
        self._workers: dict[str, WorkerIdentityV105] = {}
        self._used_tokens: set[str] = set()
        self._used_nonces: set[str] = set()
        self._revoked_certificates: set[str] = set()
        self._containments: dict[str, ContainmentRecordV105] = {}
        self._containment_epoch = 0

    def _validate_attestation(self, attestation: KubernetesAttestationV105) -> None:
        if attestation.cluster not in self.policy.allowed_clusters:
            raise EnrollmentErrorV105("cluster not allowed")
        if attestation.namespace not in self.policy.allowed_namespaces:
            raise EnrollmentErrorV105("namespace not allowed")
        if attestation.service_account not in self.policy.allowed_service_accounts:
            raise EnrollmentErrorV105("service account not allowed")
        if attestation.zone not in self.policy.allowed_zones:
            raise EnrollmentErrorV105("zone not allowed")
        if attestation.audience != "astra-worker-enrollment-v105":
            raise EnrollmentErrorV105("attestation audience mismatch")

    def enroll(self, token: SignedEnrollmentV105, attestation: KubernetesAttestationV105, now: datetime) -> WorkerIdentityV105:
        now = _aware(now, "now")
        with self._lock:
            self.keyring.verify(token.key_id, token.unsigned_payload(), token.signature, now)
            if token.generation != self.policy.generation:
                raise EnrollmentErrorV105("policy generation mismatch")
            if now < token.not_before or now >= token.expires_at:
                raise EnrollmentErrorV105("enrollment token outside time window")
            if token.token_id in self._used_tokens or token.nonce in self._used_nonces:
                raise ReplayErrorV105("enrollment replay")
            if token.certificate_fingerprint in self._revoked_certificates:
                raise EnrollmentErrorV105("certificate revoked")
            if token.attestation_digest != _digest(attestation):
                raise EnrollmentErrorV105("attestation digest mismatch")
            self._validate_attestation(attestation)
            existing = self._workers.get(token.worker_id)
            if existing is not None and existing.state not in (WorkerStateV105.STOPPED, WorkerStateV105.REVOKED):
                raise EnrollmentErrorV105("worker already enrolled")
            identity_generation = (existing.identity_generation + 1) if existing else 1
            worker = WorkerIdentityV105(
                worker_id=token.worker_id,
                deployment_id=attestation.deployment_id,
                cluster=attestation.cluster,
                namespace=attestation.namespace,
                service_account=attestation.service_account,
                zone=attestation.zone,
                certificate_fingerprint=token.certificate_fingerprint,
                identity_generation=identity_generation,
                state=WorkerStateV105.ACTIVE,
                enrolled_at=now,
                last_heartbeat_at=now,
            )
            self._workers[token.worker_id] = worker
            self._used_tokens.add(token.token_id)
            self._used_nonces.add(token.nonce)
            self.journal.append("WORKER_ENROLLED", now, worker_id=token.worker_id, details={"generation": identity_generation})
            return worker

    def rotate_identity(self, worker_id: str, new_certificate_fingerprint: str, *, operator_a: str, operator_b: str, now: datetime) -> WorkerIdentityV105:
        now = _aware(now, "now")
        _validate_hex64(new_certificate_fingerprint, "new_certificate_fingerprint")
        if not operator_a or not operator_b or operator_a == operator_b:
            raise WorkerStateErrorV105("dual control required")
        with self._lock:
            worker = self._require_worker(worker_id)
            if worker.state not in (WorkerStateV105.ACTIVE, WorkerStateV105.DRAINING):
                raise WorkerStateErrorV105("worker identity cannot be rotated")
            if new_certificate_fingerprint == worker.certificate_fingerprint or new_certificate_fingerprint in self._revoked_certificates:
                raise WorkerStateErrorV105("invalid replacement certificate")
            self._revoked_certificates.add(worker.certificate_fingerprint)
            updated = replace(worker, certificate_fingerprint=new_certificate_fingerprint, identity_generation=worker.identity_generation + 1)
            self._workers[worker_id] = updated
            self.journal.append("WORKER_IDENTITY_ROTATED", now, worker_id=worker_id, details={"operators": sorted((operator_a, operator_b)), "generation": updated.identity_generation})
            return updated

    def revoke_worker(self, worker_id: str, *, reason: str, operator_id: str, now: datetime) -> WorkerIdentityV105:
        now = _aware(now, "now")
        if not reason.strip() or not operator_id.strip():
            raise WorkerStateErrorV105("reason and operator required")
        with self._lock:
            worker = self._require_worker(worker_id)
            if worker.state is WorkerStateV105.REVOKED:
                return worker
            self._revoked_certificates.add(worker.certificate_fingerprint)
            updated = replace(worker, state=WorkerStateV105.REVOKED, recovery_required=worker.active_claims > 0)
            self._workers[worker_id] = updated
            self.journal.append("WORKER_REVOKED", now, worker_id=worker_id, details={"reason": reason, "operator_id": operator_id})
            return updated

    def heartbeat(self, worker_id: str, certificate_fingerprint: str, sequence: int, observed_at: datetime) -> WorkerIdentityV105:
        observed_at = _aware(observed_at, "observed_at")
        with self._lock:
            worker = self._require_worker(worker_id)
            if worker.state in (WorkerStateV105.STOPPED, WorkerStateV105.REVOKED, WorkerStateV105.QUARANTINED):
                raise WorkerStateErrorV105("worker cannot heartbeat")
            if certificate_fingerprint != worker.certificate_fingerprint or certificate_fingerprint in self._revoked_certificates:
                raise WorkerStateErrorV105("worker certificate mismatch")
            if sequence <= worker.heartbeat_sequence:
                raise WorkerStateErrorV105("heartbeat sequence regression")
            if observed_at < worker.last_heartbeat_at:
                raise WorkerStateErrorV105("heartbeat time regression")
            updated = replace(worker, heartbeat_sequence=sequence, last_heartbeat_at=observed_at)
            self._workers[worker_id] = updated
            self.journal.append("WORKER_HEARTBEAT", observed_at, worker_id=worker_id, details={"sequence": sequence})
            return updated

    def assert_claimable(self, worker_id: str, now: datetime) -> WorkerIdentityV105:
        now = _aware(now, "now")
        with self._lock:
            worker = self._require_worker(worker_id)
            if worker.state is not WorkerStateV105.ACTIVE:
                raise WorkerStateErrorV105("worker is not active")
            if now - worker.last_heartbeat_at > self.policy.heartbeat_ttl:
                raise WorkerStateErrorV105("worker heartbeat stale")
            if self.is_contained(worker):
                raise ContainmentErrorV105("worker contained")
            return worker

    def assign_claim(self, worker_id: str, now: datetime) -> WorkerIdentityV105:
        now = _aware(now, "now")
        with self._lock:
            worker = self.assert_claimable(worker_id, now)
            updated = replace(worker, active_claims=worker.active_claims + 1)
            self._workers[worker_id] = updated
            self.journal.append("CLAIM_ASSIGNED", now, worker_id=worker_id, details={"active_claims": updated.active_claims})
            return updated

    def complete_claim(self, worker_id: str, now: datetime) -> WorkerIdentityV105:
        now = _aware(now, "now")
        with self._lock:
            worker = self._require_worker(worker_id)
            if worker.active_claims <= 0:
                raise WorkerStateErrorV105("no active claim")
            updated = replace(worker, active_claims=worker.active_claims - 1)
            self._workers[worker_id] = updated
            self.journal.append("CLAIM_COMPLETED", now, worker_id=worker_id, details={"active_claims": updated.active_claims})
            return updated

    def begin_drain(self, worker_id: str, now: datetime) -> WorkerIdentityV105:
        now = _aware(now, "now")
        with self._lock:
            worker = self._require_worker(worker_id)
            if worker.state is WorkerStateV105.DRAINING:
                return worker
            if worker.state is not WorkerStateV105.ACTIVE:
                raise WorkerStateErrorV105("worker cannot begin drain")
            updated = replace(worker, state=WorkerStateV105.DRAINING, drain_deadline=now + self.policy.drain_timeout)
            self._workers[worker_id] = updated
            self.journal.append("DRAIN_STARTED", now, worker_id=worker_id, details={"deadline": updated.drain_deadline})
            return updated

    def finalize_drain(self, worker_id: str, *, evidence_flushed: bool, now: datetime) -> WorkerIdentityV105:
        now = _aware(now, "now")
        with self._lock:
            worker = self._require_worker(worker_id)
            if worker.state is not WorkerStateV105.DRAINING or worker.drain_deadline is None:
                raise WorkerStateErrorV105("worker is not draining")
            if worker.active_claims == 0 and evidence_flushed:
                updated = replace(worker, state=WorkerStateV105.STOPPED, drain_deadline=None)
                event = "DRAIN_COMPLETED"
            elif now >= worker.drain_deadline:
                updated = replace(worker, state=WorkerStateV105.QUARANTINED, recovery_required=True)
                event = "DRAIN_TIMEOUT_QUARANTINE"
            else:
                raise WorkerStateErrorV105("drain not complete")
            self._workers[worker_id] = updated
            self.journal.append(event, now, worker_id=worker_id, details={"active_claims": updated.active_claims, "evidence_flushed": evidence_flushed})
            return updated

    def activate_containment(self, containment_id: str, scope: ContainmentScopeV105, target: str, *, reason: str, now: datetime) -> ContainmentRecordV105:
        now = _aware(now, "now")
        _validate_id(containment_id, "containment_id")
        _validate_id(target, "target")
        if not reason.strip():
            raise ContainmentErrorV105("containment reason required")
        with self._lock:
            existing = self._containments.get(containment_id)
            if existing is not None:
                if existing.active and existing.scope is scope and existing.target == target and existing.reason == reason:
                    return existing
                raise ContainmentErrorV105("containment identifier conflict")
            self._containment_epoch += 1
            record = ContainmentRecordV105(containment_id, scope, target, self._containment_epoch, reason, now)
            self._containments[containment_id] = record
            self.journal.append("CONTAINMENT_ACTIVATED", now, details={"containment_id": containment_id, "scope": scope.value, "target": target, "epoch": record.epoch})
            return record

    def release_containment(
        self,
        containment_id: str,
        *,
        operator_a: str,
        operator_b: str,
        cleanup_evidence_digest: str,
        cleanup_confirmed: bool,
        now: datetime,
    ) -> ContainmentRecordV105:
        now = _aware(now, "now")
        _validate_hex64(cleanup_evidence_digest, "cleanup_evidence_digest")
        if not cleanup_confirmed or not operator_a or not operator_b or operator_a == operator_b:
            raise ContainmentErrorV105("dual-control cleanup confirmation required")
        with self._lock:
            record = self._containments.get(containment_id)
            if record is None:
                raise ContainmentErrorV105("unknown containment")
            if not record.active:
                return record
            impacted = [worker for worker in self._workers.values() if self._matches(record, worker)]
            if any(worker.active_claims > 0 or worker.recovery_required for worker in impacted):
                raise ContainmentErrorV105("impacted workers are not clean")
            updated = replace(
                record,
                active=False,
                released_at=now,
                release_evidence_digest=cleanup_evidence_digest,
                released_by=(operator_a, operator_b),
            )
            self._containments[containment_id] = updated
            self.journal.append("CONTAINMENT_RELEASED", now, details={"containment_id": containment_id, "epoch": record.epoch, "operators": sorted((operator_a, operator_b))})
            return updated

    def is_contained(self, worker: WorkerIdentityV105) -> bool:
        return any(record.active and self._matches(record, worker) for record in self._containments.values())

    def _matches(self, record: ContainmentRecordV105, worker: WorkerIdentityV105) -> bool:
        if record.scope is ContainmentScopeV105.FLEET:
            return record.target == self.policy.fleet_id
        if record.scope is ContainmentScopeV105.ZONE:
            return record.target == worker.zone
        if record.scope is ContainmentScopeV105.DEPLOYMENT:
            return record.target == worker.deployment_id
        return record.target == worker.worker_id

    def _require_worker(self, worker_id: str) -> WorkerIdentityV105:
        worker = self._workers.get(worker_id)
        if worker is None:
            raise WorkerStateErrorV105("unknown worker")
        return worker

    def worker(self, worker_id: str) -> WorkerIdentityV105:
        with self._lock:
            return self._require_worker(worker_id)

    def workers(self) -> tuple[WorkerIdentityV105, ...]:
        with self._lock:
            return tuple(sorted(self._workers.values(), key=lambda item: item.worker_id))

    def active_containments(self) -> tuple[ContainmentRecordV105, ...]:
        with self._lock:
            return tuple(sorted((item for item in self._containments.values() if item.active), key=lambda item: item.epoch))


@dataclass(frozen=True, slots=True)
class FleetMetricsV105:
    queue_depth: int
    current_replicas: int
    ready_workers: int
    active_claims: int
    draining_workers: int
    crash_count: int
    dlq_depth: int
    control_plane_ready: bool
    postgres_ready: bool
    object_store_ready: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        for name in ("queue_depth", "current_replicas", "ready_workers", "active_claims", "draining_workers", "crash_count", "dlq_depth"):
            if getattr(self, name) < 0:
                raise PolicyErrorV105(f"{name} cannot be negative")
        _aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class ScaleDecisionV105:
    decision_id: str
    action: ScaleActionV105
    current_replicas: int
    desired_replicas: int
    raw_desired_replicas: int
    reason: str
    observed_at: datetime
    policy_digest: str
    previous_decision_digest: str
    digest: str


class ControlledAutoscalerV105:
    def __init__(self, policy: FleetPolicyV105) -> None:
        self.policy = policy
        self._lock = threading.RLock()
        self._last_scale_at: datetime | None = None
        self._history: list[int] = []
        self._previous_digest = "0" * 64
        self._sequence = 0

    def decide(self, metrics: FleetMetricsV105, *, containment_active: bool = False) -> ScaleDecisionV105:
        now = _aware(metrics.observed_at, "observed_at")
        with self._lock:
            raw = max(self.policy.min_replicas, math.ceil(metrics.queue_depth / self.policy.target_queue_per_worker))
            raw = max(raw, metrics.active_claims, metrics.draining_workers)
            raw = min(raw, self.policy.max_replicas)
            self._history.append(raw)
            if len(self._history) > self.policy.stabilization_samples:
                self._history.pop(0)

            current = metrics.current_replicas
            desired = current
            reason = "TARGET_STABLE"
            action = ScaleActionV105.HOLD
            dependencies_ready = metrics.control_plane_ready and metrics.postgres_ready and metrics.object_store_ready
            budget_ok = metrics.crash_count <= self.policy.crash_budget and metrics.dlq_depth <= self.policy.dlq_budget

            if containment_active:
                reason = "CONTAINMENT_ACTIVE"
            elif not dependencies_ready:
                reason = "DEPENDENCY_NOT_READY"
            elif not budget_ok:
                reason = "INCIDENT_BUDGET_EXHAUSTED"
            elif raw > current:
                if self._last_scale_at is not None and now - self._last_scale_at < self.policy.scale_up_cooldown:
                    reason = "SCALE_UP_COOLDOWN"
                else:
                    desired = min(raw, current + self.policy.max_scale_up_step, self.policy.max_replicas)
                    action = ScaleActionV105.SCALE_UP
                    reason = "QUEUE_PRESSURE"
            elif raw < current:
                stable_down = len(self._history) >= self.policy.stabilization_samples and max(self._history) < current
                if not stable_down:
                    reason = "SCALE_DOWN_STABILIZING"
                elif self._last_scale_at is not None and now - self._last_scale_at < self.policy.scale_down_cooldown:
                    reason = "SCALE_DOWN_COOLDOWN"
                elif metrics.active_claims > 0 or metrics.draining_workers > 0:
                    reason = "ACTIVE_WORK_PROTECTS_CAPACITY"
                else:
                    desired = max(raw, current - self.policy.max_scale_down_step, self.policy.min_replicas)
                    action = ScaleActionV105.SCALE_DOWN
                    reason = "SUSTAINED_LOW_LOAD"

            if desired != current:
                self._last_scale_at = now
            self._sequence += 1
            body = {
                "decision_id": f"scale-{self._sequence:08d}",
                "action": action,
                "current_replicas": current,
                "desired_replicas": desired,
                "raw_desired_replicas": raw,
                "reason": reason,
                "observed_at": now,
                "policy_digest": self.policy.digest,
                "previous_decision_digest": self._previous_digest,
            }
            digest = _digest(body)
            decision = ScaleDecisionV105(**body, digest=digest)
            self._previous_digest = digest
            return decision


@dataclass(frozen=True, slots=True)
class FleetReadinessV105:
    policy_sealed: bool
    signing_keys_valid: bool
    active_workers: int
    stale_workers: int
    draining_workers: int
    quarantined_workers: int
    active_containments: int
    eligible_for_read_only_fleet: bool
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False


def readiness_snapshot(policy: FleetPolicyV105, keyring: RotatingKeyRingV105, registry: FleetRegistryV105, now: datetime) -> FleetReadinessV105:
    now = _aware(now, "now")
    workers = registry.workers()
    active = [worker for worker in workers if worker.state is WorkerStateV105.ACTIVE]
    stale = [worker for worker in active if now - worker.last_heartbeat_at > policy.heartbeat_ttl]
    draining = [worker for worker in workers if worker.state is WorkerStateV105.DRAINING]
    quarantined = [worker for worker in workers if worker.state is WorkerStateV105.QUARANTINED]
    keys = keyring.public_snapshot()
    signing_valid = sum(item["status"] == KeyStatusV105.ACTIVE.value for item in keys) == 1
    containments = registry.active_containments()
    eligible = bool(active) and not stale and not quarantined and not containments and signing_valid
    return FleetReadinessV105(
        policy_sealed=bool(policy.digest),
        signing_keys_valid=signing_valid,
        active_workers=len(active),
        stale_workers=len(stale),
        draining_workers=len(draining),
        quarantined_workers=len(quarantined),
        active_containments=len(containments),
        eligible_for_read_only_fleet=eligible,
    )
