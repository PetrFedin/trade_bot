from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_KEYRING_SCHEMA = "astra-qualification-keyring-v1"
_ENVELOPE_SCHEMA = "astra-qualification-signature-v1"
_ALLOWED_BACKENDS = {"KMS", "HSM"}


@runtime_checkable
class QualificationSigningProvider(Protocol):
    @property
    def key_id(self) -> str: ...

    @property
    def backend(self) -> object: ...

    @property
    def generation(self) -> int: ...

    def public_key_bytes(self) -> bytes: ...

    def sign(self, payload: bytes) -> bytes: ...


def _backend_value(value: object) -> str:
    normalized = getattr(value, "value", value)
    if not isinstance(normalized, str) or normalized not in _ALLOWED_BACKENDS:
        raise ValueError("qualification signing backend must be KMS or HSM")
    return normalized


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized


def _b64decode(value: str, *, expected_length: int, name: str) -> bytes:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError(f"invalid {name}") from exc
    if len(decoded) != expected_length:
        raise ValueError(f"invalid {name} length")
    return decoded


def _b64encode(value: bytes, *, expected_length: int, name: str) -> str:
    if len(value) != expected_length:
        raise ValueError(f"invalid {name}")
    return base64.b64encode(value).decode("ascii")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=lambda item: (
            _aware(item, "datetime").isoformat()
            if isinstance(item, datetime)
            else item.value
            if hasattr(item, "value")
            else asdict(item)
        ),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class QualificationSigningKeyDescriptor:
    key_id: str
    owner_id: str
    backend: str
    generation: int
    public_key_b64: str
    not_before: datetime
    not_after: datetime
    revoked_at: datetime | None = None

    def validate(self) -> None:
        for name, value in (("key_id", self.key_id), ("owner_id", self.owner_id)):
            if not value.strip():
                raise ValueError(f"{name} is required")
        _backend_value(self.backend)
        if self.generation < 1:
            raise ValueError("qualification key generation must be positive")
        _b64decode(self.public_key_b64, expected_length=32, name="public key")
        start = _aware(self.not_before, "not_before")
        end = _aware(self.not_after, "not_after")
        if start >= end:
            raise ValueError("invalid qualification key validity interval")
        if self.revoked_at is not None and _aware(self.revoked_at, "revoked_at") < start:
            raise ValueError("qualification key revocation predates validity")

    @property
    def public_key_bytes(self) -> bytes:
        self.validate()
        return _b64decode(self.public_key_b64, expected_length=32, name="public key")

    def is_active(self, observed_at: datetime) -> bool:
        current = _aware(observed_at, "observed_at")
        return (
            _aware(self.not_before, "not_before")
            <= current
            < _aware(self.not_after, "not_after")
            and (
                self.revoked_at is None
                or current < _aware(self.revoked_at, "revoked_at")
            )
        )

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "key_id": self.key_id,
            "owner_id": self.owner_id,
            "backend": self.backend,
            "generation": self.generation,
            "public_key_b64": self.public_key_b64,
            "not_before": _aware(self.not_before, "not_before").isoformat(),
            "not_after": _aware(self.not_after, "not_after").isoformat(),
            "revoked_at": (
                None
                if self.revoked_at is None
                else _aware(self.revoked_at, "revoked_at").isoformat()
            ),
        }


@dataclass(frozen=True)
class QualificationKeyringSnapshot:
    generation: int
    issued_at: datetime
    expires_at: datetime
    root_key_id: str
    keys: tuple[QualificationSigningKeyDescriptor, ...]
    root_signature_b64: str
    schema_version: str = _KEYRING_SCHEMA

    def validate(self) -> None:
        if self.schema_version != _KEYRING_SCHEMA:
            raise ValueError("qualification keyring schema mismatch")
        if self.generation < 1:
            raise ValueError("qualification keyring generation must be positive")
        if not self.root_key_id.strip():
            raise ValueError("qualification root_key_id is required")
        if not self.keys:
            raise ValueError("qualification keyring cannot be empty")
        issued = _aware(self.issued_at, "issued_at")
        expires = _aware(self.expires_at, "expires_at")
        if issued >= expires:
            raise ValueError("invalid qualification keyring validity interval")
        ids = [item.key_id for item in self.keys]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate qualification key id")
        for item in self.keys:
            item.validate()
            if item.generation > self.generation:
                raise ValueError("qualification key generation exceeds keyring generation")
        _b64decode(self.root_signature_b64, expected_length=64, name="root signature")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "domain": "astra.qualification.keyring.v1",
            "generation": self.generation,
            "issued_at": _aware(self.issued_at, "issued_at").isoformat(),
            "expires_at": _aware(self.expires_at, "expires_at").isoformat(),
            "root_key_id": self.root_key_id,
            "keys": [
                item.payload()
                for item in sorted(self.keys, key=lambda value: value.key_id)
            ],
        }

    @property
    def snapshot_sha256(self) -> str:
        self.validate()
        return _sha256(self.unsigned_payload())

    @classmethod
    def sign(
        cls,
        *,
        generation: int,
        issued_at: datetime,
        expires_at: datetime,
        keys: Sequence[QualificationSigningKeyDescriptor],
        root_provider: QualificationSigningProvider,
    ) -> QualificationKeyringSnapshot:
        unsigned = cls(
            generation=generation,
            issued_at=issued_at,
            expires_at=expires_at,
            root_key_id=root_provider.key_id,
            keys=tuple(keys),
            root_signature_b64=_b64encode(
                b"\0" * 64,
                expected_length=64,
                name="root signature",
            ),
        )
        unsigned.validate()
        signature = root_provider.sign(_canonical_bytes(unsigned.unsigned_payload()))
        signed = cls(
            generation=generation,
            issued_at=issued_at,
            expires_at=expires_at,
            root_key_id=root_provider.key_id,
            keys=tuple(keys),
            root_signature_b64=_b64encode(
                signature,
                expected_length=64,
                name="root signature",
            ),
        )
        signed.validate()
        return signed


@dataclass(frozen=True)
class VerifiedQualificationKeyring:
    generation: int
    snapshot_sha256: str
    keys: Mapping[str, QualificationSigningKeyDescriptor]

    def __post_init__(self) -> None:
        _digest(self.snapshot_sha256, "snapshot_sha256")
        object.__setattr__(self, "keys", MappingProxyType(dict(self.keys)))

    def require_key(
        self,
        *,
        key_id: str,
        key_generation: int,
        observed_at: datetime,
    ) -> QualificationSigningKeyDescriptor:
        descriptor = self.keys.get(key_id)
        if descriptor is None:
            raise ValueError("unknown qualification signing key")
        if descriptor.generation != key_generation:
            raise ValueError("qualification signing key generation mismatch")
        if not descriptor.is_active(observed_at):
            raise ValueError("qualification signing key is inactive or revoked")
        return descriptor


def verify_qualification_keyring(
    snapshot: QualificationKeyringSnapshot,
    *,
    trusted_root_public_keys: Mapping[str, bytes],
    previous_generation: int,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> VerifiedQualificationKeyring:
    snapshot.validate()
    if snapshot.generation <= previous_generation:
        raise ValueError("qualification keyring generation is not monotonic")
    current = _aware(observed_at, "observed_at")
    skew = timedelta(seconds=max_clock_skew_seconds)
    if current + skew < _aware(snapshot.issued_at, "issued_at"):
        raise ValueError("qualification keyring is not yet valid")
    if current - skew >= _aware(snapshot.expires_at, "expires_at"):
        raise ValueError("qualification keyring has expired")
    root = trusted_root_public_keys.get(snapshot.root_key_id)
    if root is None:
        raise ValueError("untrusted qualification keyring root")
    try:
        Ed25519PublicKey.from_public_bytes(root).verify(
            _b64decode(
                snapshot.root_signature_b64,
                expected_length=64,
                name="root signature",
            ),
            _canonical_bytes(snapshot.unsigned_payload()),
        )
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("invalid qualification keyring root signature") from exc
    return VerifiedQualificationKeyring(
        generation=snapshot.generation,
        snapshot_sha256=snapshot.snapshot_sha256,
        keys={item.key_id: item for item in snapshot.keys},
    )


@dataclass(frozen=True)
class QualificationSignatureEnvelope:
    signature_id: str
    domain: str
    payload_sha256: str
    key_id: str
    key_generation: int
    keyring_generation: int
    issued_at: datetime
    expires_at: datetime
    nonce: str
    signature_b64: str
    schema_version: str = _ENVELOPE_SCHEMA

    def validate(self) -> None:
        if self.schema_version != _ENVELOPE_SCHEMA:
            raise ValueError("qualification signature schema mismatch")
        for name, value in (
            ("signature_id", self.signature_id),
            ("domain", self.domain),
            ("key_id", self.key_id),
            ("nonce", self.nonce),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        _digest(self.payload_sha256, "payload_sha256")
        if self.key_generation < 1 or self.keyring_generation < 1:
            raise ValueError("qualification signature generations must be positive")
        if _aware(self.issued_at, "issued_at") >= _aware(self.expires_at, "expires_at"):
            raise ValueError("invalid qualification signature validity interval")
        _b64decode(self.signature_b64, expected_length=64, name="signature")

    def unsigned_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "signature_id": self.signature_id,
            "domain": self.domain,
            "payload_sha256": self.payload_sha256,
            "key_id": self.key_id,
            "key_generation": self.key_generation,
            "keyring_generation": self.keyring_generation,
            "issued_at": _aware(self.issued_at, "issued_at").isoformat(),
            "expires_at": _aware(self.expires_at, "expires_at").isoformat(),
            "nonce": self.nonce,
        }

    @property
    def envelope_sha256(self) -> str:
        return _sha256(
            {
                **self.unsigned_payload(),
                "signature_b64": self.signature_b64,
            }
        )


def sign_qualification_payload(
    *,
    provider: QualificationSigningProvider,
    descriptor: QualificationSigningKeyDescriptor,
    keyring_generation: int,
    signature_id: str,
    domain: str,
    payload_sha256: str,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
    max_lifetime_seconds: int = 600,
) -> QualificationSignatureEnvelope:
    descriptor.validate()
    if provider.key_id != descriptor.key_id:
        raise ValueError("qualification provider key mismatch")
    if _backend_value(provider.backend) != _backend_value(descriptor.backend):
        raise ValueError("qualification provider backend mismatch")
    if provider.generation != descriptor.generation:
        raise ValueError("qualification provider generation mismatch")
    if provider.public_key_bytes() != descriptor.public_key_bytes:
        raise ValueError("qualification provider public key mismatch")
    issued = _aware(issued_at, "issued_at")
    expires = _aware(expires_at, "expires_at")
    if max_lifetime_seconds <= 0 or (expires - issued).total_seconds() > max_lifetime_seconds:
        raise ValueError("qualification signature lifetime exceeds policy")
    if not descriptor.is_active(issued) or expires > _aware(
        descriptor.not_after,
        "descriptor.not_after",
    ):
        raise ValueError("qualification signature exceeds key validity")
    unsigned = QualificationSignatureEnvelope(
        signature_id=signature_id,
        domain=domain,
        payload_sha256=payload_sha256,
        key_id=descriptor.key_id,
        key_generation=descriptor.generation,
        keyring_generation=keyring_generation,
        issued_at=issued,
        expires_at=expires,
        nonce=nonce,
        signature_b64=_b64encode(b"\0" * 64, expected_length=64, name="signature"),
    )
    signature = provider.sign(_canonical_bytes(unsigned.unsigned_payload()))
    signed = QualificationSignatureEnvelope(
        signature_id=signature_id,
        domain=domain,
        payload_sha256=payload_sha256,
        key_id=descriptor.key_id,
        key_generation=descriptor.generation,
        keyring_generation=keyring_generation,
        issued_at=issued,
        expires_at=expires,
        nonce=nonce,
        signature_b64=_b64encode(signature, expected_length=64, name="signature"),
    )
    signed.validate()
    return signed


def verify_qualification_signature(
    envelope: QualificationSignatureEnvelope,
    *,
    keyring: VerifiedQualificationKeyring,
    expected_domain: str,
    expected_payload_sha256: str,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> QualificationSigningKeyDescriptor:
    envelope.validate()
    _digest(expected_payload_sha256, "expected_payload_sha256")
    if envelope.domain != expected_domain:
        raise ValueError("qualification signature domain mismatch")
    if envelope.payload_sha256 != expected_payload_sha256:
        raise ValueError("qualification signature payload mismatch")
    if envelope.keyring_generation != keyring.generation:
        raise ValueError("qualification signature keyring generation mismatch")
    current = _aware(observed_at, "observed_at")
    skew = timedelta(seconds=max_clock_skew_seconds)
    if current + skew < _aware(envelope.issued_at, "issued_at"):
        raise ValueError("qualification signature is from the future")
    if current - skew >= _aware(envelope.expires_at, "expires_at"):
        raise ValueError("qualification signature has expired")
    descriptor = keyring.require_key(
        key_id=envelope.key_id,
        key_generation=envelope.key_generation,
        observed_at=current,
    )
    try:
        Ed25519PublicKey.from_public_bytes(descriptor.public_key_bytes).verify(
            _b64decode(envelope.signature_b64, expected_length=64, name="signature"),
            _canonical_bytes(envelope.unsigned_payload()),
        )
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("invalid qualification Ed25519 signature") from exc
    return descriptor


class QualificationSignatureReplayLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signature_ids: set[str] = set()
        self._nonces: set[str] = set()

    def consume(self, envelope: QualificationSignatureEnvelope) -> None:
        with self._lock:
            if (
                envelope.signature_id in self._signature_ids
                or envelope.nonce in self._nonces
            ):
                raise ValueError("qualification signature replay detected")
            self._signature_ids.add(envelope.signature_id)
            self._nonces.add(envelope.nonce)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._signature_ids)
