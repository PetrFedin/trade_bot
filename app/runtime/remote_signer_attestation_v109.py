from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import ssl
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.runtime.signing_authority_v108 import Ed25519SigningProviderV108, SigningBackendV108

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")


class RemoteSignerErrorV109(RuntimeError):
    pass


class RemoteSignerValidationErrorV109(RemoteSignerErrorV109):
    pass


class RemoteSignerPolicyErrorV109(RemoteSignerErrorV109):
    pass


class RemoteSignerVerificationErrorV109(RemoteSignerErrorV109):
    pass


class RemoteSignerRejectedErrorV109(RemoteSignerErrorV109):
    pass


class RemoteSignerUncertainErrorV109(RemoteSignerErrorV109):
    pass


class RemoteSignerQuarantinedErrorV109(RemoteSignerErrorV109):
    pass


class RemoteSignerConflictV109(RemoteSignerErrorV109):
    pass


class RemoteSignStateV109(StrEnum):
    CREATED = "CREATED"
    DISPATCH_STARTED = "DISPATCH_STARTED"
    SIGNED = "SIGNED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    QUARANTINED = "QUARANTINED"


def _ensure_utc(value: datetime, name: str = "datetime") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RemoteSignerValidationErrorV109(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise RemoteSignerValidationErrorV109(f"invalid {name}")


def _validate_domain(value: str) -> None:
    if not isinstance(value, str) or not _DOMAIN_RE.fullmatch(value):
        raise RemoteSignerValidationErrorV109("invalid domain")


def _validate_digest(value: str, name: str) -> None:
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise RemoteSignerValidationErrorV109(f"invalid {name}")


def _decode_b64(value: str, *, expected_length: int | None = None, name: str) -> bytes:
    if not isinstance(value, str):
        raise RemoteSignerValidationErrorV109(f"invalid {name}")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise RemoteSignerValidationErrorV109(f"invalid {name}") from exc
    if expected_length is not None and len(decoded) != expected_length:
        raise RemoteSignerValidationErrorV109(f"invalid {name} length")
    return decoded


def _encode_b64(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise RemoteSignerValidationErrorV109("value must be bytes")
    return base64.b64encode(value).decode("ascii")


def canonical_bytes_v109(value: Any) -> bytes:
    def default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return _timestamp(obj)
        if isinstance(obj, Enum):
            return obj.value
        if hasattr(obj, "to_payload"):
            return obj.to_payload()
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        raise TypeError(type(obj).__name__)

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=default).encode("utf-8")


def digest_v109(value: Any) -> str:
    return hashlib.sha256(canonical_bytes_v109(value)).hexdigest()


def bytes_digest_v109(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise RemoteSignerValidationErrorV109("payload must be bytes")
    return hashlib.sha256(value).hexdigest()


def _validate_origin(value: str) -> str:
    if not isinstance(value, str):
        raise RemoteSignerValidationErrorV109("endpoint origin must be a string")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise RemoteSignerValidationErrorV109("endpoint must be an exact HTTPS origin")
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"https://{host}{port}"


@dataclass(frozen=True, slots=True)
class RemoteSignerPolicySnapshotV109:
    provider_id: str
    generation: int
    endpoint_origin: str
    mtls_identity_ref: str
    signing_key_id: str
    signing_public_key_b64: str
    attestation_key_id: str
    attestation_public_key_b64: str
    allowed_hardware_clusters: tuple[str, ...]
    allowed_firmware_measurements: tuple[str, ...]
    predecessor_keyring_digest: str
    request_ttl_seconds: int
    timeout_seconds: float
    max_response_bytes: int
    issued_at: datetime
    expires_at: datetime
    root_key_id: str
    root_signature_b64: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.provider_id, "provider_id"),
            (self.mtls_identity_ref, "mtls_identity_ref"),
            (self.signing_key_id, "signing_key_id"),
            (self.attestation_key_id, "attestation_key_id"),
            (self.root_key_id, "root_key_id"),
        ):
            _validate_id(value, name)
        if self.generation <= 0:
            raise RemoteSignerValidationErrorV109("policy generation must be positive")
        object.__setattr__(self, "endpoint_origin", _validate_origin(self.endpoint_origin))
        _decode_b64(self.signing_public_key_b64, expected_length=32, name="signing public key")
        _decode_b64(
            self.attestation_public_key_b64, expected_length=32, name="attestation public key"
        )
        _decode_b64(self.root_signature_b64, expected_length=64, name="root signature")
        _validate_digest(self.predecessor_keyring_digest, "predecessor_keyring_digest")
        if self.request_ttl_seconds <= 0 or self.request_ttl_seconds > 3600:
            raise RemoteSignerValidationErrorV109("invalid request TTL")
        if not (0 < self.timeout_seconds <= 60):
            raise RemoteSignerValidationErrorV109("invalid timeout")
        if not (256 <= self.max_response_bytes <= 4 * 1024 * 1024):
            raise RemoteSignerValidationErrorV109("invalid response size limit")
        if not self.allowed_hardware_clusters or not self.allowed_firmware_measurements:
            raise RemoteSignerValidationErrorV109("attestation allowlists must not be empty")
        if len(set(self.allowed_hardware_clusters)) != len(self.allowed_hardware_clusters):
            raise RemoteSignerValidationErrorV109("duplicate hardware cluster")
        if len(set(self.allowed_firmware_measurements)) != len(self.allowed_firmware_measurements):
            raise RemoteSignerValidationErrorV109("duplicate firmware measurement")
        for item in self.allowed_hardware_clusters:
            _validate_id(item, "hardware_cluster")
        for measurement in self.allowed_firmware_measurements:
            _validate_digest(measurement, "firmware_measurement")
        if _ensure_utc(self.issued_at, "issued_at") >= _ensure_utc(self.expires_at, "expires_at"):
            raise RemoteSignerValidationErrorV109("invalid policy validity interval")

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "schema": 109,
            "domain": "astra.remote-signer.policy.v109",
            "provider_id": self.provider_id,
            "generation": self.generation,
            "endpoint_origin": self.endpoint_origin,
            "mtls_identity_ref": self.mtls_identity_ref,
            "signing_key_id": self.signing_key_id,
            "signing_public_key_b64": self.signing_public_key_b64,
            "attestation_key_id": self.attestation_key_id,
            "attestation_public_key_b64": self.attestation_public_key_b64,
            "allowed_hardware_clusters": sorted(self.allowed_hardware_clusters),
            "allowed_firmware_measurements": sorted(self.allowed_firmware_measurements),
            "predecessor_keyring_digest": self.predecessor_keyring_digest,
            "request_ttl_seconds": self.request_ttl_seconds,
            "timeout_seconds": self.timeout_seconds,
            "max_response_bytes": self.max_response_bytes,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "root_key_id": self.root_key_id,
        }

    @property
    def snapshot_digest(self) -> str:
        return digest_v109(self.unsigned_payload())

    def to_payload(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "root_signature_b64": self.root_signature_b64}


@dataclass(frozen=True, slots=True)
class VerifiedRemoteSignerPolicyV109:
    snapshot: RemoteSignerPolicySnapshotV109
    verified_at: datetime

    def __post_init__(self) -> None:
        _ensure_utc(self.verified_at, "verified_at")

    @property
    def provider_id(self) -> str:
        return self.snapshot.provider_id

    @property
    def generation(self) -> int:
        return self.snapshot.generation

    @property
    def policy_digest(self) -> str:
        return self.snapshot.snapshot_digest


def verify_remote_signer_policy_v109(
    snapshot: RemoteSignerPolicySnapshotV109,
    *,
    trusted_root_public_keys: Mapping[str, bytes],
    expected_predecessor_keyring_digest: str,
    minimum_generation: int,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> VerifiedRemoteSignerPolicyV109:
    if minimum_generation < 0 or max_clock_skew_seconds < 0:
        raise RemoteSignerValidationErrorV109("invalid verification bounds")
    _validate_digest(expected_predecessor_keyring_digest, "expected_predecessor_keyring_digest")
    if snapshot.predecessor_keyring_digest != expected_predecessor_keyring_digest:
        raise RemoteSignerPolicyErrorV109("predecessor keyring digest mismatch")
    if snapshot.generation <= minimum_generation:
        raise RemoteSignerPolicyErrorV109("policy generation is not monotonic")
    current = _ensure_utc(observed_at)
    skew = timedelta(seconds=max_clock_skew_seconds)
    if current + skew < _ensure_utc(snapshot.issued_at):
        raise RemoteSignerPolicyErrorV109("policy is not yet valid")
    if current - skew >= _ensure_utc(snapshot.expires_at):
        raise RemoteSignerPolicyErrorV109("policy has expired")
    root = trusted_root_public_keys.get(snapshot.root_key_id)
    if not isinstance(root, bytes) or len(root) != 32:
        raise RemoteSignerPolicyErrorV109("untrusted policy root")
    try:
        Ed25519PublicKey.from_public_bytes(root).verify(
            _decode_b64(snapshot.root_signature_b64, expected_length=64, name="root signature"),
            canonical_bytes_v109(snapshot.unsigned_payload()),
        )
    except (ValueError, InvalidSignature) as exc:
        raise RemoteSignerPolicyErrorV109("invalid policy root signature") from exc
    return VerifiedRemoteSignerPolicyV109(snapshot=snapshot, verified_at=current)


@dataclass(frozen=True, slots=True)
class RemoteSignRequestV109:
    request_id: str
    nonce: str
    provider_id: str
    policy_generation: int
    policy_digest: str
    key_id: str
    key_generation: int
    keyring_generation: int
    purpose: str
    domain: str
    payload_digest: str
    created_at: datetime
    deadline_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.request_id, "request_id"),
            (self.nonce, "nonce"),
            (self.provider_id, "provider_id"),
            (self.key_id, "key_id"),
        ):
            _validate_id(value, name)
        if self.policy_generation <= 0 or self.key_generation <= 0 or self.keyring_generation <= 0:
            raise RemoteSignerValidationErrorV109("request generations must be positive")
        _validate_digest(self.policy_digest, "policy_digest")
        _validate_digest(self.payload_digest, "payload_digest")
        _validate_domain(self.domain)
        if not isinstance(self.purpose, str) or not self.purpose:
            raise RemoteSignerValidationErrorV109("invalid purpose")
        if _ensure_utc(self.created_at) >= _ensure_utc(self.deadline_at):
            raise RemoteSignerValidationErrorV109("invalid request deadline")

    @property
    def request_digest(self) -> str:
        return digest_v109(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": 109,
            "request_id": self.request_id,
            "nonce": self.nonce,
            "provider_id": self.provider_id,
            "policy_generation": self.policy_generation,
            "policy_digest": self.policy_digest,
            "key_id": self.key_id,
            "key_generation": self.key_generation,
            "keyring_generation": self.keyring_generation,
            "purpose": self.purpose,
            "domain": self.domain,
            "payload_digest": self.payload_digest,
            "created_at": self.created_at,
            "deadline_at": self.deadline_at,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RemoteSignRequestV109:
        def dt(name: str) -> datetime:
            raw = payload[name]
            if not isinstance(raw, str):
                raise RemoteSignerValidationErrorV109(f"invalid {name}")
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))

        return cls(
            request_id=str(payload["request_id"]),
            nonce=str(payload["nonce"]),
            provider_id=str(payload["provider_id"]),
            policy_generation=int(payload["policy_generation"]),
            policy_digest=str(payload["policy_digest"]),
            key_id=str(payload["key_id"]),
            key_generation=int(payload["key_generation"]),
            keyring_generation=int(payload["keyring_generation"]),
            purpose=str(payload["purpose"]),
            domain=str(payload["domain"]),
            payload_digest=str(payload["payload_digest"]),
            created_at=dt("created_at"),
            deadline_at=dt("deadline_at"),
        )


@dataclass(frozen=True, slots=True)
class RemoteSignerAuditCheckpointV109:
    provider_id: str
    policy_generation: int
    audit_sequence: int
    hardware_signing_counter: int
    audit_chain_root: str
    observed_at: datetime

    def __post_init__(self) -> None:
        _validate_id(self.provider_id, "provider_id")
        if (
            self.policy_generation <= 0
            or self.audit_sequence < 0
            or self.hardware_signing_counter < 0
        ):
            raise RemoteSignerValidationErrorV109("invalid audit checkpoint")
        _validate_digest(self.audit_chain_root, "audit_chain_root")
        _ensure_utc(self.observed_at)


@dataclass(frozen=True, slots=True)
class ProviderAttestationV109:
    request_id: str
    request_digest: str
    signature_digest: str
    provider_id: str
    policy_generation: int
    policy_digest: str
    signing_key_id: str
    attestation_key_id: str
    hardware_cluster_id: str
    firmware_measurement: str
    hardware_signing_counter: int
    audit_sequence: int
    audit_event_digest: str
    audit_chain_root: str
    attested_at: datetime
    attestation_signature_b64: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.request_id, "request_id"),
            (self.provider_id, "provider_id"),
            (self.signing_key_id, "signing_key_id"),
            (self.attestation_key_id, "attestation_key_id"),
            (self.hardware_cluster_id, "hardware_cluster_id"),
        ):
            _validate_id(value, name)
        for value, name in (
            (self.request_digest, "request_digest"),
            (self.signature_digest, "signature_digest"),
            (self.policy_digest, "policy_digest"),
            (self.firmware_measurement, "firmware_measurement"),
            (self.audit_event_digest, "audit_event_digest"),
            (self.audit_chain_root, "audit_chain_root"),
        ):
            _validate_digest(value, name)
        if (
            self.policy_generation <= 0
            or self.hardware_signing_counter <= 0
            or self.audit_sequence <= 0
        ):
            raise RemoteSignerValidationErrorV109("invalid attestation counters")
        _ensure_utc(self.attested_at)
        _decode_b64(
            self.attestation_signature_b64, expected_length=64, name="attestation signature"
        )

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "schema": 109,
            "domain": "astra.remote-signer.attestation.v109",
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "signature_digest": self.signature_digest,
            "provider_id": self.provider_id,
            "policy_generation": self.policy_generation,
            "policy_digest": self.policy_digest,
            "signing_key_id": self.signing_key_id,
            "attestation_key_id": self.attestation_key_id,
            "hardware_cluster_id": self.hardware_cluster_id,
            "firmware_measurement": self.firmware_measurement,
            "hardware_signing_counter": self.hardware_signing_counter,
            "audit_sequence": self.audit_sequence,
            "audit_event_digest": self.audit_event_digest,
            "audit_chain_root": self.audit_chain_root,
            "attested_at": self.attested_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.unsigned_payload(),
            "attestation_signature_b64": self.attestation_signature_b64,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ProviderAttestationV109:
        raw_time = payload["attested_at"]
        if not isinstance(raw_time, str):
            raise RemoteSignerValidationErrorV109("invalid attested_at")
        return cls(
            request_id=str(payload["request_id"]),
            request_digest=str(payload["request_digest"]),
            signature_digest=str(payload["signature_digest"]),
            provider_id=str(payload["provider_id"]),
            policy_generation=int(payload["policy_generation"]),
            policy_digest=str(payload["policy_digest"]),
            signing_key_id=str(payload["signing_key_id"]),
            attestation_key_id=str(payload["attestation_key_id"]),
            hardware_cluster_id=str(payload["hardware_cluster_id"]),
            firmware_measurement=str(payload["firmware_measurement"]),
            hardware_signing_counter=int(payload["hardware_signing_counter"]),
            audit_sequence=int(payload["audit_sequence"]),
            audit_event_digest=str(payload["audit_event_digest"]),
            audit_chain_root=str(payload["audit_chain_root"]),
            attested_at=datetime.fromisoformat(raw_time.replace("Z", "+00:00")),
            attestation_signature_b64=str(payload["attestation_signature_b64"]),
        )


@dataclass(frozen=True, slots=True)
class VerifiedRemoteSignResultV109:
    signature: bytes
    attestation: ProviderAttestationV109
    checkpoint: RemoteSignerAuditCheckpointV109


def verify_remote_sign_result_v109(
    *,
    policy: VerifiedRemoteSignerPolicyV109,
    request: RemoteSignRequestV109,
    payload: bytes,
    signature: bytes,
    attestation: ProviderAttestationV109,
    previous_checkpoint: RemoteSignerAuditCheckpointV109 | None,
    observed_at: datetime,
) -> VerifiedRemoteSignResultV109:
    current = _ensure_utc(observed_at)
    snapshot = policy.snapshot
    if current >= _ensure_utc(request.deadline_at):
        raise RemoteSignerQuarantinedErrorV109("request reconciliation deadline exceeded")
    if bytes_digest_v109(payload) != request.payload_digest:
        raise RemoteSignerVerificationErrorV109("payload digest mismatch")
    if (
        request.provider_id != snapshot.provider_id
        or request.policy_generation != snapshot.generation
        or request.policy_digest != policy.policy_digest
        or request.key_id != snapshot.signing_key_id
    ):
        raise RemoteSignerVerificationErrorV109("request policy binding mismatch")
    if len(signature) != 64:
        raise RemoteSignerVerificationErrorV109("invalid payload signature length")
    expected_bindings = {
        "request_id": request.request_id,
        "request_digest": request.request_digest,
        "signature_digest": bytes_digest_v109(signature),
        "provider_id": snapshot.provider_id,
        "policy_generation": snapshot.generation,
        "policy_digest": policy.policy_digest,
        "signing_key_id": snapshot.signing_key_id,
        "attestation_key_id": snapshot.attestation_key_id,
    }
    for name, expected in expected_bindings.items():
        if getattr(attestation, name) != expected:
            raise RemoteSignerVerificationErrorV109(f"attestation binding mismatch: {name}")
    if attestation.hardware_cluster_id not in snapshot.allowed_hardware_clusters:
        raise RemoteSignerVerificationErrorV109("hardware cluster is not allowed")
    if attestation.firmware_measurement not in snapshot.allowed_firmware_measurements:
        raise RemoteSignerVerificationErrorV109("firmware measurement is not allowed")
    if _ensure_utc(attestation.attested_at) > current + timedelta(seconds=5):
        raise RemoteSignerVerificationErrorV109("attestation time is in the future")
    if previous_checkpoint is not None:
        if previous_checkpoint.provider_id != snapshot.provider_id:
            raise RemoteSignerVerificationErrorV109("audit checkpoint provider mismatch")
        if previous_checkpoint.policy_generation > snapshot.generation:
            raise RemoteSignerVerificationErrorV109("audit checkpoint policy generation rollback")
        if attestation.audit_sequence <= previous_checkpoint.audit_sequence:
            raise RemoteSignerVerificationErrorV109("provider audit sequence is not monotonic")
        if attestation.hardware_signing_counter <= previous_checkpoint.hardware_signing_counter:
            raise RemoteSignerVerificationErrorV109("hardware signing counter is not monotonic")
    try:
        Ed25519PublicKey.from_public_bytes(
            _decode_b64(
                snapshot.signing_public_key_b64, expected_length=32, name="signing public key"
            )
        ).verify(signature, payload)
        Ed25519PublicKey.from_public_bytes(
            _decode_b64(
                snapshot.attestation_public_key_b64,
                expected_length=32,
                name="attestation public key",
            )
        ).verify(
            _decode_b64(
                attestation.attestation_signature_b64,
                expected_length=64,
                name="attestation signature",
            ),
            canonical_bytes_v109(attestation.unsigned_payload()),
        )
    except (ValueError, InvalidSignature) as exc:
        raise RemoteSignerVerificationErrorV109(
            "provider result signature verification failed"
        ) from exc
    checkpoint = RemoteSignerAuditCheckpointV109(
        provider_id=snapshot.provider_id,
        policy_generation=snapshot.generation,
        audit_sequence=attestation.audit_sequence,
        hardware_signing_counter=attestation.hardware_signing_counter,
        audit_chain_root=attestation.audit_chain_root,
        observed_at=current,
    )
    return VerifiedRemoteSignResultV109(
        signature=signature, attestation=attestation, checkpoint=checkpoint
    )


class RemoteSignerRepositoryV109(Protocol):
    def install_verified_policy(self, policy: VerifiedRemoteSignerPolicyV109) -> None: ...

    def create_request_with_outbox(
        self, request: RemoteSignRequestV109, payload: bytes
    ) -> None: ...

    def mark_dispatch_started(
        self, request_id: str, *, worker_id: str, observed_at: datetime
    ) -> None: ...

    def record_signed(
        self,
        request: RemoteSignRequestV109,
        result: VerifiedRemoteSignResultV109,
        *,
        observed_at: datetime,
    ) -> None: ...

    def record_rejected(self, request_id: str, *, reason: str, observed_at: datetime) -> None: ...

    def record_uncertain(self, request_id: str, *, reason: str, observed_at: datetime) -> None: ...

    def record_quarantined(
        self, request_id: str, *, reason: str, observed_at: datetime
    ) -> None: ...

    def load_request(self, request_id: str) -> RemoteSignRequestV109: ...

    def load_checkpoint(self, provider_id: str) -> RemoteSignerAuditCheckpointV109 | None: ...


@dataclass(slots=True)
class _MemoryRequestV109:
    request: RemoteSignRequestV109
    payload: bytes
    state: RemoteSignStateV109


class InMemoryRemoteSignerRepositoryV109:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._policies: dict[tuple[str, int], str] = {}
        self._requests: dict[str, _MemoryRequestV109] = {}
        self._nonces: set[str] = set()
        self._checkpoints: dict[str, RemoteSignerAuditCheckpointV109] = {}

    def install_verified_policy(self, policy: VerifiedRemoteSignerPolicyV109) -> None:
        key = (policy.provider_id, policy.generation)
        with self._lock:
            current = self._policies.get(key)
            if current is not None and current != policy.policy_digest:
                raise RemoteSignerConflictV109("policy generation equivocation")
            self._policies[key] = policy.policy_digest

    def create_request_with_outbox(self, request: RemoteSignRequestV109, payload: bytes) -> None:
        with self._lock:
            if (
                self._policies.get((request.provider_id, request.policy_generation))
                != request.policy_digest
            ):
                raise RemoteSignerConflictV109("request references an unverified policy")
            if request.request_id in self._requests or request.nonce in self._nonces:
                raise RemoteSignerConflictV109("request replay detected")
            self._requests[request.request_id] = _MemoryRequestV109(
                request=request,
                payload=bytes(payload),
                state=RemoteSignStateV109.CREATED,
            )
            self._nonces.add(request.nonce)

    def mark_dispatch_started(
        self, request_id: str, *, worker_id: str, observed_at: datetime
    ) -> None:
        _validate_id(worker_id, "worker_id")
        _ensure_utc(observed_at)
        with self._lock:
            record = self._requests[request_id]
            if record.state is not RemoteSignStateV109.CREATED:
                raise RemoteSignerConflictV109("dispatch already claimed")
            record.state = RemoteSignStateV109.DISPATCH_STARTED

    def _transition(self, request_id: str, state: RemoteSignStateV109) -> None:
        with self._lock:
            record = self._requests[request_id]
            if record.state in {
                RemoteSignStateV109.SIGNED,
                RemoteSignStateV109.REJECTED,
                RemoteSignStateV109.QUARANTINED,
            }:
                raise RemoteSignerConflictV109("terminal request cannot transition")
            record.state = state

    def record_signed(
        self,
        request: RemoteSignRequestV109,
        result: VerifiedRemoteSignResultV109,
        *,
        observed_at: datetime,
    ) -> None:
        _ensure_utc(observed_at)
        with self._lock:
            record = self._requests[request.request_id]
            if record.request.request_digest != request.request_digest:
                raise RemoteSignerConflictV109("request digest conflict")
            previous = self._checkpoints.get(request.provider_id)
            if previous is not None and (
                result.checkpoint.audit_sequence <= previous.audit_sequence
                or result.checkpoint.hardware_signing_counter <= previous.hardware_signing_counter
            ):
                raise RemoteSignerConflictV109("audit checkpoint rollback")
            if record.state not in {
                RemoteSignStateV109.DISPATCH_STARTED,
                RemoteSignStateV109.UNCERTAIN,
            }:
                raise RemoteSignerConflictV109("request is not signable")
            self._checkpoints[request.provider_id] = result.checkpoint
            record.state = RemoteSignStateV109.SIGNED

    def record_rejected(self, request_id: str, *, reason: str, observed_at: datetime) -> None:
        if not reason:
            raise RemoteSignerValidationErrorV109("rejection reason is required")
        _ensure_utc(observed_at)
        self._transition(request_id, RemoteSignStateV109.REJECTED)

    def record_uncertain(self, request_id: str, *, reason: str, observed_at: datetime) -> None:
        if not reason:
            raise RemoteSignerValidationErrorV109("uncertain reason is required")
        _ensure_utc(observed_at)
        self._transition(request_id, RemoteSignStateV109.UNCERTAIN)

    def record_quarantined(self, request_id: str, *, reason: str, observed_at: datetime) -> None:
        if not reason:
            raise RemoteSignerValidationErrorV109("quarantine reason is required")
        _ensure_utc(observed_at)
        self._transition(request_id, RemoteSignStateV109.QUARANTINED)

    def load_request(self, request_id: str) -> RemoteSignRequestV109:
        with self._lock:
            return self._requests[request_id].request

    def load_checkpoint(self, provider_id: str) -> RemoteSignerAuditCheckpointV109 | None:
        with self._lock:
            return self._checkpoints.get(provider_id)

    def state(self, request_id: str) -> RemoteSignStateV109:
        with self._lock:
            return self._requests[request_id].state


class MutualTlsContextProviderV109(Protocol):
    def ssl_context(self, identity_ref: str) -> ssl.SSLContext: ...


@dataclass(frozen=True, slots=True)
class RemoteSignerHttpResponseV109:
    status: int
    body: bytes


class _NoRedirectV109(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class RemoteSignerHttpClientV109:
    def __init__(
        self, policy: VerifiedRemoteSignerPolicyV109, contexts: MutualTlsContextProviderV109
    ) -> None:
        self._policy = policy
        context = contexts.ssl_context(policy.snapshot.mtls_identity_ref)
        if context.check_hostname is not True or context.verify_mode != ssl.CERT_REQUIRED:
            raise RemoteSignerValidationErrorV109("mTLS context must verify peer and hostname")
        if context.minimum_version < ssl.TLSVersion.TLSv1_3:
            raise RemoteSignerValidationErrorV109("TLS 1.3 minimum is required")
        self._opener = build_opener(HTTPSHandler(context=context), _NoRedirectV109())

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            raise RemoteSignerValidationErrorV109("remote signer path must be absolute")
        return f"{self._policy.snapshot.endpoint_origin}{path}"

    def _request(
        self, method: str, path: str, body: bytes | None = None
    ) -> RemoteSignerHttpResponseV109:
        request = Request(
            self._url(path),
            data=body,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(
                request, timeout=self._policy.snapshot.timeout_seconds
            ) as response:
                payload = response.read(self._policy.snapshot.max_response_bytes + 1)
                if len(payload) > self._policy.snapshot.max_response_bytes:
                    raise RemoteSignerUncertainErrorV109("remote signer response exceeded bound")
                return RemoteSignerHttpResponseV109(status=int(response.status), body=payload)
        except HTTPError as exc:
            payload = exc.read(self._policy.snapshot.max_response_bytes + 1)
            if len(payload) > self._policy.snapshot.max_response_bytes:
                raise RemoteSignerUncertainErrorV109(
                    "remote signer error response exceeded bound"
                ) from exc
            return RemoteSignerHttpResponseV109(status=int(exc.code), body=payload)
        except (URLError, TimeoutError, OSError) as exc:
            raise RemoteSignerUncertainErrorV109(
                "remote signer transport outcome is ambiguous"
            ) from exc

    def post_sign(
        self, request: RemoteSignRequestV109, payload: bytes
    ) -> RemoteSignerHttpResponseV109:
        body = canonical_bytes_v109(
            {"request": request.to_payload(), "payload_b64": _encode_b64(payload)}
        )
        return self._request("POST", "/v1/signing/requests", body)

    def get_request(self, request_id: str) -> RemoteSignerHttpResponseV109:
        _validate_id(request_id, "request_id")
        return self._request("GET", f"/v1/signing/requests/{quote(request_id, safe='')}")


class RemoteSignerClientV109(Protocol):
    def post_sign(
        self, request: RemoteSignRequestV109, payload: bytes
    ) -> RemoteSignerHttpResponseV109: ...

    def get_request(self, request_id: str) -> RemoteSignerHttpResponseV109: ...


_DETERMINISTIC_REJECTION_STATUSES_V109 = frozenset({400, 401, 403, 404, 422})
_AMBIGUOUS_STATUSES_V109 = frozenset({202, 204, 301, 302, 303, 307, 308, 408, 409, 425, 429})


def _parse_success_v109(
    response: RemoteSignerHttpResponseV109,
) -> tuple[bytes, ProviderAttestationV109]:
    try:
        payload = json.loads(response.body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("response is not an object")
        signature = _decode_b64(
            str(payload["signature_b64"]), expected_length=64, name="payload signature"
        )
        attestation_payload = payload["attestation"]
        if not isinstance(attestation_payload, dict):
            raise TypeError("attestation is not an object")
        attestation = ProviderAttestationV109.from_payload(attestation_payload)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        RemoteSignerValidationErrorV109,
    ) as exc:
        raise RemoteSignerUncertainErrorV109("malformed successful remote signer response") from exc
    return signature, attestation


class RemoteEd25519SigningProviderV109(Ed25519SigningProviderV108):
    def __init__(
        self,
        *,
        policy: VerifiedRemoteSignerPolicyV109,
        repository: RemoteSignerRepositoryV109,
        client: RemoteSignerClientV109,
        worker_id: str,
        key_generation: int,
        keyring_generation: int,
        purpose: str,
        domain: str,
        clock: Callable[[], datetime],
        request_id_factory: Callable[[], str],
        nonce_factory: Callable[[], str],
    ) -> None:
        _validate_id(worker_id, "worker_id")
        _validate_domain(domain)
        if key_generation <= 0 or keyring_generation <= 0:
            raise RemoteSignerValidationErrorV109("provider generations must be positive")
        self._policy = policy
        self._repository = repository
        self._client = client
        self._worker_id = worker_id
        self._key_generation = key_generation
        self._keyring_generation = keyring_generation
        self._purpose = purpose
        self._domain = domain
        self._clock = clock
        self._request_id_factory = request_id_factory
        self._nonce_factory = nonce_factory

    @property
    def key_id(self) -> str:
        return self._policy.snapshot.signing_key_id

    @property
    def backend(self) -> SigningBackendV108:
        return SigningBackendV108.KMS

    @property
    def generation(self) -> int:
        return self._key_generation

    def public_key_bytes(self) -> bytes:
        return _decode_b64(
            self._policy.snapshot.signing_public_key_b64,
            expected_length=32,
            name="signing public key",
        )

    def _new_request(self, payload: bytes) -> RemoteSignRequestV109:
        now = _ensure_utc(self._clock())
        return RemoteSignRequestV109(
            request_id=self._request_id_factory(),
            nonce=self._nonce_factory(),
            provider_id=self._policy.provider_id,
            policy_generation=self._policy.generation,
            policy_digest=self._policy.policy_digest,
            key_id=self.key_id,
            key_generation=self._key_generation,
            keyring_generation=self._keyring_generation,
            purpose=self._purpose,
            domain=self._domain,
            payload_digest=bytes_digest_v109(payload),
            created_at=now,
            deadline_at=now + timedelta(seconds=self._policy.snapshot.request_ttl_seconds),
        )

    def _verify_response(
        self,
        request: RemoteSignRequestV109,
        payload: bytes,
        response: RemoteSignerHttpResponseV109,
        observed_at: datetime,
    ) -> VerifiedRemoteSignResultV109:
        signature, attestation = _parse_success_v109(response)
        return verify_remote_sign_result_v109(
            policy=self._policy,
            request=request,
            payload=payload,
            signature=signature,
            attestation=attestation,
            previous_checkpoint=self._repository.load_checkpoint(request.provider_id),
            observed_at=observed_at,
        )

    def sign(self, payload: bytes) -> bytes:
        if not isinstance(payload, bytes) or not payload:
            raise RemoteSignerValidationErrorV109("payload must be non-empty bytes")
        request = self._new_request(payload)
        self._repository.create_request_with_outbox(request, payload)
        dispatch_at = _ensure_utc(self._clock())
        self._repository.mark_dispatch_started(
            request.request_id,
            worker_id=self._worker_id,
            observed_at=dispatch_at,
        )
        try:
            response = self._client.post_sign(request, payload)
        except RemoteSignerUncertainErrorV109 as exc:
            self._repository.record_uncertain(
                request.request_id,
                reason=str(exc),
                observed_at=_ensure_utc(self._clock()),
            )
            raise
        observed_at = _ensure_utc(self._clock())
        if response.status in (200, 201):
            try:
                result = self._verify_response(request, payload, response, observed_at)
            except RemoteSignerQuarantinedErrorV109:
                self._repository.record_quarantined(
                    request.request_id,
                    reason="request deadline exceeded during verification",
                    observed_at=observed_at,
                )
                raise
            except (RemoteSignerUncertainErrorV109, RemoteSignerVerificationErrorV109) as exc:
                self._repository.record_uncertain(
                    request.request_id,
                    reason=str(exc),
                    observed_at=observed_at,
                )
                raise RemoteSignerUncertainErrorV109(str(exc)) from exc
            self._repository.record_signed(request, result, observed_at=observed_at)
            return result.signature
        if response.status in _DETERMINISTIC_REJECTION_STATUSES_V109:
            self._repository.record_rejected(
                request.request_id,
                reason=f"remote signer rejected status {response.status}",
                observed_at=observed_at,
            )
            raise RemoteSignerRejectedErrorV109(f"remote signer rejected status {response.status}")
        if (
            response.status in _AMBIGUOUS_STATUSES_V109
            or 500 <= response.status <= 599
            or 300 <= response.status <= 399
        ):
            reason = f"ambiguous remote signer status {response.status}"
        else:
            reason = f"unexpected remote signer status {response.status}"
        self._repository.record_uncertain(
            request.request_id, reason=reason, observed_at=observed_at
        )
        raise RemoteSignerUncertainErrorV109(reason)

    def reconcile(self, request_id: str, payload: bytes) -> bytes:
        request = self._repository.load_request(request_id)
        now = _ensure_utc(self._clock())
        if now >= _ensure_utc(request.deadline_at):
            self._repository.record_quarantined(
                request_id,
                reason="signed reconciliation deadline exceeded",
                observed_at=now,
            )
            raise RemoteSignerQuarantinedErrorV109("signed reconciliation deadline exceeded")
        response = self._client.get_request(request_id)
        if response.status not in (200, 201):
            raise RemoteSignerUncertainErrorV109(
                f"GET reconciliation incomplete: {response.status}"
            )
        try:
            result = self._verify_response(request, payload, response, now)
        except RemoteSignerQuarantinedErrorV109:
            self._repository.record_quarantined(
                request_id,
                reason="request deadline exceeded during reconciliation",
                observed_at=now,
            )
            raise
        except (RemoteSignerUncertainErrorV109, RemoteSignerVerificationErrorV109) as exc:
            raise RemoteSignerUncertainErrorV109(str(exc)) from exc
        self._repository.record_signed(request, result, observed_at=now)
        return result.signature
