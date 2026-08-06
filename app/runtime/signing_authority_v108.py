from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import base64
import binascii
import hashlib
import json
import re
import threading
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

UTC = timezone.utc
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class SigningAuthorityErrorV108(RuntimeError):
    pass


class SigningValidationErrorV108(SigningAuthorityErrorV108):
    pass


class KeyringVerificationErrorV108(SigningAuthorityErrorV108):
    pass


class SignatureVerificationErrorV108(SigningAuthorityErrorV108):
    pass


class SignatureReplayErrorV108(SigningAuthorityErrorV108):
    pass


class SigningBackendV108(str, Enum):
    KMS = "KMS"
    HSM = "HSM"


class SigningPurposeV108(str, Enum):
    KEYRING_ROOT = "KEYRING_ROOT"
    RELEASE_APPROVAL = "RELEASE_APPROVAL"
    RISK_APPROVAL = "RISK_APPROVAL"
    CONTROLLER_COMMAND = "CONTROLLER_COMMAND"
    EXECUTOR_RECEIPT = "EXECUTOR_RECEIPT"


def _ensure_utc(value: datetime, name: str = "datetime") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SigningValidationErrorV108(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise SigningValidationErrorV108(f"invalid {name}")


def _validate_domain(value: str) -> None:
    if not isinstance(value, str) or not _DOMAIN_RE.fullmatch(value):
        raise SigningValidationErrorV108("invalid signature domain")


def _validate_digest(value: str, name: str) -> None:
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise SigningValidationErrorV108(f"invalid {name}")


def _decode_b64(value: str, *, expected_length: int, name: str) -> bytes:
    if not isinstance(value, str):
        raise SigningValidationErrorV108(f"invalid {name}")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise SigningValidationErrorV108(f"invalid {name}") from exc
    if len(decoded) != expected_length:
        raise SigningValidationErrorV108(f"invalid {name} length")
    return decoded


def _encode_b64(value: bytes, *, expected_length: int, name: str) -> str:
    if not isinstance(value, bytes) or len(value) != expected_length:
        raise SigningValidationErrorV108(f"invalid {name}")
    return base64.b64encode(value).decode("ascii")


def canonical_bytes_v108(value: Any) -> bytes:
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


def digest_v108(value: Any) -> str:
    return hashlib.sha256(canonical_bytes_v108(value)).hexdigest()


@runtime_checkable
class Ed25519SigningProviderV108(Protocol):
    @property
    def key_id(self) -> str: ...

    @property
    def backend(self) -> SigningBackendV108: ...

    @property
    def generation(self) -> int: ...

    def public_key_bytes(self) -> bytes: ...

    def sign(self, payload: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class SigningKeyDescriptorV108:
    key_id: str
    owner_id: str
    purpose: SigningPurposeV108
    backend: SigningBackendV108
    generation: int
    public_key_b64: str
    not_before: datetime
    not_after: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_id(self.key_id, "key_id")
        _validate_id(self.owner_id, "owner_id")
        if self.generation <= 0:
            raise SigningValidationErrorV108("key generation must be positive")
        _decode_b64(self.public_key_b64, expected_length=32, name="public key")
        start = _ensure_utc(self.not_before, "not_before")
        end = _ensure_utc(self.not_after, "not_after")
        if start >= end:
            raise SigningValidationErrorV108("invalid key validity interval")
        if self.revoked_at is not None:
            revoked = _ensure_utc(self.revoked_at, "revoked_at")
            if revoked < start:
                raise SigningValidationErrorV108("revocation predates key validity")

    @property
    def public_key_bytes(self) -> bytes:
        return _decode_b64(self.public_key_b64, expected_length=32, name="public key")

    def is_active(self, observed_at: datetime) -> bool:
        current = _ensure_utc(observed_at)
        if not (_ensure_utc(self.not_before) <= current < _ensure_utc(self.not_after)):
            return False
        return self.revoked_at is None or current < _ensure_utc(self.revoked_at)

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RootSignedKeyringSnapshotV108:
    generation: int
    issued_at: datetime
    expires_at: datetime
    root_key_id: str
    keys: tuple[SigningKeyDescriptorV108, ...]
    root_signature_b64: str

    def __post_init__(self) -> None:
        if self.generation <= 0:
            raise SigningValidationErrorV108("keyring generation must be positive")
        _validate_id(self.root_key_id, "root_key_id")
        issued = _ensure_utc(self.issued_at, "issued_at")
        expires = _ensure_utc(self.expires_at, "expires_at")
        if issued >= expires:
            raise SigningValidationErrorV108("invalid keyring validity interval")
        if not self.keys:
            raise SigningValidationErrorV108("keyring must not be empty")
        key_ids = [item.key_id for item in self.keys]
        if len(key_ids) != len(set(key_ids)):
            raise SigningValidationErrorV108("duplicate key_id in keyring")
        for item in self.keys:
            if item.generation > self.generation:
                raise SigningValidationErrorV108("key generation exceeds keyring generation")
        _decode_b64(self.root_signature_b64, expected_length=64, name="root signature")

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "schema": 108,
            "domain": "astra.keyring.snapshot.v108",
            "generation": self.generation,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "root_key_id": self.root_key_id,
            "keys": [item.to_payload() for item in sorted(self.keys, key=lambda key: key.key_id)],
        }

    @property
    def snapshot_digest(self) -> str:
        return digest_v108(self.unsigned_payload())

    def to_payload(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "root_signature_b64": self.root_signature_b64}

    @classmethod
    def sign(
        cls,
        *,
        generation: int,
        issued_at: datetime,
        expires_at: datetime,
        keys: Sequence[SigningKeyDescriptorV108],
        root_provider: Ed25519SigningProviderV108,
    ) -> "RootSignedKeyringSnapshotV108":
        if root_provider.backend not in {SigningBackendV108.KMS, SigningBackendV108.HSM}:
            raise SigningValidationErrorV108("root provider must be KMS or HSM backed")
        unsigned = cls(
            generation=generation,
            issued_at=issued_at,
            expires_at=expires_at,
            root_key_id=root_provider.key_id,
            keys=tuple(keys),
            root_signature_b64=_encode_b64(b"\0" * 64, expected_length=64, name="root signature"),
        )
        signature = root_provider.sign(canonical_bytes_v108(unsigned.unsigned_payload()))
        encoded = _encode_b64(signature, expected_length=64, name="root signature")
        signed = cls(
            generation=generation,
            issued_at=issued_at,
            expires_at=expires_at,
            root_key_id=root_provider.key_id,
            keys=tuple(keys),
            root_signature_b64=encoded,
        )
        try:
            Ed25519PublicKey.from_public_bytes(root_provider.public_key_bytes()).verify(
                signature, canonical_bytes_v108(signed.unsigned_payload())
            )
        except (ValueError, InvalidSignature) as exc:
            raise SignatureVerificationErrorV108("root provider returned an invalid signature") from exc
        return signed


@dataclass(frozen=True, slots=True)
class VerifiedKeyringV108:
    generation: int
    snapshot_digest: str
    keys: Mapping[str, SigningKeyDescriptorV108] = field(repr=False)

    def __post_init__(self) -> None:
        _validate_digest(self.snapshot_digest, "snapshot_digest")
        object.__setattr__(self, "keys", MappingProxyType(dict(self.keys)))

    def require_key(
        self,
        *,
        key_id: str,
        purpose: SigningPurposeV108,
        key_generation: int,
        observed_at: datetime,
    ) -> SigningKeyDescriptorV108:
        key = self.keys.get(key_id)
        if key is None:
            raise SignatureVerificationErrorV108("unknown signing key")
        if key.purpose != purpose:
            raise SignatureVerificationErrorV108("signing key purpose mismatch")
        if key.generation != key_generation:
            raise SignatureVerificationErrorV108("signing key generation mismatch")
        if not key.is_active(observed_at):
            raise SignatureVerificationErrorV108("signing key is inactive or revoked")
        return key


def verify_keyring_snapshot_v108(
    snapshot: RootSignedKeyringSnapshotV108,
    *,
    trusted_root_public_keys: Mapping[str, bytes],
    previous_generation: int,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> VerifiedKeyringV108:
    if previous_generation < 0:
        raise SigningValidationErrorV108("previous_generation must be non-negative")
    if snapshot.generation <= previous_generation:
        raise KeyringVerificationErrorV108("keyring generation is not monotonic")
    if max_clock_skew_seconds < 0:
        raise SigningValidationErrorV108("max_clock_skew_seconds must be non-negative")
    current = _ensure_utc(observed_at)
    skew = timedelta(seconds=max_clock_skew_seconds)
    if current + skew < _ensure_utc(snapshot.issued_at):
        raise KeyringVerificationErrorV108("keyring is not yet valid")
    if current - skew >= _ensure_utc(snapshot.expires_at):
        raise KeyringVerificationErrorV108("keyring has expired")
    root_bytes = trusted_root_public_keys.get(snapshot.root_key_id)
    if root_bytes is None:
        raise KeyringVerificationErrorV108("untrusted keyring root")
    if not isinstance(root_bytes, bytes) or len(root_bytes) != 32:
        raise SigningValidationErrorV108("invalid trusted root public key")
    try:
        Ed25519PublicKey.from_public_bytes(root_bytes).verify(
            _decode_b64(snapshot.root_signature_b64, expected_length=64, name="root signature"),
            canonical_bytes_v108(snapshot.unsigned_payload()),
        )
    except (ValueError, InvalidSignature) as exc:
        raise KeyringVerificationErrorV108("invalid keyring root signature") from exc
    required = {
        SigningPurposeV108.RELEASE_APPROVAL,
        SigningPurposeV108.RISK_APPROVAL,
        SigningPurposeV108.CONTROLLER_COMMAND,
        SigningPurposeV108.EXECUTOR_RECEIPT,
    }
    active_purposes = {key.purpose for key in snapshot.keys if key.is_active(current)}
    missing = required - active_purposes
    if missing:
        raise KeyringVerificationErrorV108(f"keyring lacks active purposes: {sorted(item.value for item in missing)}")
    return VerifiedKeyringV108(
        generation=snapshot.generation,
        snapshot_digest=snapshot.snapshot_digest,
        keys={key.key_id: key for key in snapshot.keys},
    )


@dataclass(frozen=True, slots=True)
class SignatureEnvelopeV108:
    signature_id: str
    purpose: SigningPurposeV108
    domain: str
    payload_digest: str
    key_id: str
    key_generation: int
    keyring_generation: int
    issued_at: datetime
    expires_at: datetime
    nonce: str
    signature_b64: str

    def __post_init__(self) -> None:
        for value, name in ((self.signature_id, "signature_id"), (self.key_id, "key_id"), (self.nonce, "nonce")):
            _validate_id(value, name)
        _validate_domain(self.domain)
        _validate_digest(self.payload_digest, "payload_digest")
        if self.key_generation <= 0 or self.keyring_generation <= 0:
            raise SigningValidationErrorV108("signature generations must be positive")
        issued = _ensure_utc(self.issued_at, "issued_at")
        expires = _ensure_utc(self.expires_at, "expires_at")
        if issued >= expires:
            raise SigningValidationErrorV108("invalid signature validity interval")
        _decode_b64(self.signature_b64, expected_length=64, name="signature")

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "schema": 108,
            "signature_id": self.signature_id,
            "purpose": self.purpose,
            "domain": self.domain,
            "payload_digest": self.payload_digest,
            "key_id": self.key_id,
            "key_generation": self.key_generation,
            "keyring_generation": self.keyring_generation,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    @property
    def envelope_digest(self) -> str:
        return digest_v108({**self.unsigned_payload(), "signature_b64": self.signature_b64})

    def to_payload(self) -> dict[str, Any]:
        return {**self.unsigned_payload(), "signature_b64": self.signature_b64}


def sign_envelope_v108(
    *,
    provider: Ed25519SigningProviderV108,
    descriptor: SigningKeyDescriptorV108,
    keyring_generation: int,
    signature_id: str,
    purpose: SigningPurposeV108,
    domain: str,
    payload_digest: str,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
    max_lifetime_seconds: int = 600,
) -> SignatureEnvelopeV108:
    if provider.key_id != descriptor.key_id or provider.backend != descriptor.backend:
        raise SigningValidationErrorV108("provider does not match key descriptor")
    if provider.generation != descriptor.generation:
        raise SigningValidationErrorV108("provider generation does not match key descriptor")
    if provider.public_key_bytes() != descriptor.public_key_bytes:
        raise SigningValidationErrorV108("provider public key does not match key descriptor")
    if descriptor.purpose != purpose:
        raise SigningValidationErrorV108("descriptor purpose mismatch")
    issued = _ensure_utc(issued_at)
    expires = _ensure_utc(expires_at)
    if max_lifetime_seconds <= 0 or (expires - issued).total_seconds() > max_lifetime_seconds:
        raise SigningValidationErrorV108("signature lifetime exceeds policy")
    if not descriptor.is_active(issued) or expires > _ensure_utc(descriptor.not_after):
        raise SigningValidationErrorV108("signature interval exceeds key validity")
    unsigned = SignatureEnvelopeV108(
        signature_id=signature_id,
        purpose=purpose,
        domain=domain,
        payload_digest=payload_digest,
        key_id=descriptor.key_id,
        key_generation=descriptor.generation,
        keyring_generation=keyring_generation,
        issued_at=issued,
        expires_at=expires,
        nonce=nonce,
        signature_b64=_encode_b64(b"\0" * 64, expected_length=64, name="signature"),
    )
    signature = provider.sign(canonical_bytes_v108(unsigned.unsigned_payload()))
    signed = SignatureEnvelopeV108(
        signature_id=signature_id,
        purpose=purpose,
        domain=domain,
        payload_digest=payload_digest,
        key_id=descriptor.key_id,
        key_generation=descriptor.generation,
        keyring_generation=keyring_generation,
        issued_at=issued,
        expires_at=expires,
        nonce=nonce,
        signature_b64=_encode_b64(signature, expected_length=64, name="signature"),
    )
    try:
        Ed25519PublicKey.from_public_bytes(descriptor.public_key_bytes).verify(
            signature, canonical_bytes_v108(signed.unsigned_payload())
        )
    except (ValueError, InvalidSignature) as exc:
        raise SignatureVerificationErrorV108("provider returned an invalid signature") from exc
    return signed


def verify_envelope_v108(
    envelope: SignatureEnvelopeV108,
    *,
    keyring: VerifiedKeyringV108,
    expected_purpose: SigningPurposeV108,
    expected_domain: str,
    expected_payload_digest: str,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> SigningKeyDescriptorV108:
    _validate_domain(expected_domain)
    _validate_digest(expected_payload_digest, "expected_payload_digest")
    current = _ensure_utc(observed_at)
    if envelope.purpose != expected_purpose:
        raise SignatureVerificationErrorV108("signature purpose mismatch")
    if envelope.domain != expected_domain:
        raise SignatureVerificationErrorV108("signature domain mismatch")
    if envelope.payload_digest != expected_payload_digest:
        raise SignatureVerificationErrorV108("signature payload mismatch")
    if envelope.keyring_generation != keyring.generation:
        raise SignatureVerificationErrorV108("signature keyring generation mismatch")
    skew = timedelta(seconds=max_clock_skew_seconds)
    if current + skew < _ensure_utc(envelope.issued_at):
        raise SignatureVerificationErrorV108("signature is from the future")
    if current - skew >= _ensure_utc(envelope.expires_at):
        raise SignatureVerificationErrorV108("signature has expired")
    descriptor = keyring.require_key(
        key_id=envelope.key_id,
        purpose=expected_purpose,
        key_generation=envelope.key_generation,
        observed_at=current,
    )
    try:
        Ed25519PublicKey.from_public_bytes(descriptor.public_key_bytes).verify(
            _decode_b64(envelope.signature_b64, expected_length=64, name="signature"),
            canonical_bytes_v108(envelope.unsigned_payload()),
        )
    except (ValueError, InvalidSignature) as exc:
        raise SignatureVerificationErrorV108("invalid Ed25519 signature") from exc
    return descriptor


@dataclass(frozen=True, slots=True)
class RolloutAuthorizationBundleV108:
    bundle_id: str
    command_digest: str
    policy_digest: str
    predecessor_release_identity_digest: str
    keyring_generation: int
    release: SignatureEnvelopeV108
    risk: SignatureEnvelopeV108
    controller: SignatureEnvelopeV108

    def __post_init__(self) -> None:
        _validate_id(self.bundle_id, "bundle_id")
        for value, name in (
            (self.command_digest, "command_digest"),
            (self.policy_digest, "policy_digest"),
            (self.predecessor_release_identity_digest, "predecessor_release_identity_digest"),
        ):
            _validate_digest(value, name)
        if self.keyring_generation <= 0:
            raise SigningValidationErrorV108("keyring_generation must be positive")

    @property
    def authorization_digest(self) -> str:
        return authorization_payload_digest_v108(
            command_digest=self.command_digest,
            policy_digest=self.policy_digest,
            predecessor_release_identity_digest=self.predecessor_release_identity_digest,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": 108,
            "bundle_id": self.bundle_id,
            "command_digest": self.command_digest,
            "policy_digest": self.policy_digest,
            "predecessor_release_identity_digest": self.predecessor_release_identity_digest,
            "authorization_digest": self.authorization_digest,
            "keyring_generation": self.keyring_generation,
            "release": self.release.to_payload(),
            "risk": self.risk.to_payload(),
            "controller": self.controller.to_payload(),
        }

    @property
    def bundle_digest(self) -> str:
        return digest_v108(self.to_payload())


def authorization_payload_digest_v108(
    *, command_digest: str, policy_digest: str, predecessor_release_identity_digest: str
) -> str:
    for value, name in (
        (command_digest, "command_digest"),
        (policy_digest, "policy_digest"),
        (predecessor_release_identity_digest, "predecessor_release_identity_digest"),
    ):
        _validate_digest(value, name)
    return digest_v108(
        {
            "schema": 108,
            "domain": "astra.rollout.authorization.v108",
            "command_digest": command_digest,
            "policy_digest": policy_digest,
            "predecessor_release_identity_digest": predecessor_release_identity_digest,
        }
    )


class SignatureReplayLedgerV108:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signature_ids: set[str] = set()
        self._nonces: set[str] = set()

    def consume_many(self, envelopes: Sequence[SignatureEnvelopeV108]) -> None:
        signature_ids = [item.signature_id for item in envelopes]
        nonces = [item.nonce for item in envelopes]
        if len(signature_ids) != len(set(signature_ids)) or len(nonces) != len(set(nonces)):
            raise SignatureReplayErrorV108("duplicate signature id or nonce in bundle")
        with self._lock:
            if self._signature_ids.intersection(signature_ids) or self._nonces.intersection(nonces):
                raise SignatureReplayErrorV108("signature replay detected")
            self._signature_ids.update(signature_ids)
            self._nonces.update(nonces)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._signature_ids)


def verify_rollout_authorization_v108(
    bundle: RolloutAuthorizationBundleV108,
    *,
    keyring: VerifiedKeyringV108,
    observed_at: datetime,
    replay_ledger: SignatureReplayLedgerV108 | None = None,
) -> tuple[SigningKeyDescriptorV108, SigningKeyDescriptorV108, SigningKeyDescriptorV108]:
    if bundle.keyring_generation != keyring.generation:
        raise SignatureVerificationErrorV108("bundle keyring generation mismatch")
    domain = "astra.rollout.authorization.v108"
    expected = bundle.authorization_digest
    release_key = verify_envelope_v108(
        bundle.release,
        keyring=keyring,
        expected_purpose=SigningPurposeV108.RELEASE_APPROVAL,
        expected_domain=domain,
        expected_payload_digest=expected,
        observed_at=observed_at,
    )
    risk_key = verify_envelope_v108(
        bundle.risk,
        keyring=keyring,
        expected_purpose=SigningPurposeV108.RISK_APPROVAL,
        expected_domain=domain,
        expected_payload_digest=expected,
        observed_at=observed_at,
    )
    controller_key = verify_envelope_v108(
        bundle.controller,
        keyring=keyring,
        expected_purpose=SigningPurposeV108.CONTROLLER_COMMAND,
        expected_domain=domain,
        expected_payload_digest=expected,
        observed_at=observed_at,
    )
    descriptors = (release_key, risk_key, controller_key)
    if len({item.key_id for item in descriptors}) != 3:
        raise SignatureVerificationErrorV108("authorization requires distinct keys")
    if len({item.owner_id for item in descriptors}) != 3:
        raise SignatureVerificationErrorV108("authorization requires distinct owners")
    if replay_ledger is not None:
        replay_ledger.consume_many((bundle.release, bundle.risk, bundle.controller))
    return descriptors


def receipt_payload_digest_v108(
    *, receipt_digest: str, command_digest: str, authorization_bundle_digest: str
) -> str:
    for value, name in (
        (receipt_digest, "receipt_digest"),
        (command_digest, "command_digest"),
        (authorization_bundle_digest, "authorization_bundle_digest"),
    ):
        _validate_digest(value, name)
    return digest_v108(
        {
            "schema": 108,
            "domain": "astra.rollout.receipt.v108",
            "receipt_digest": receipt_digest,
            "command_digest": command_digest,
            "authorization_bundle_digest": authorization_bundle_digest,
        }
    )


@dataclass(frozen=True, slots=True)
class ReceiptAuthorizationV108:
    receipt_id: str
    receipt_digest: str
    command_digest: str
    authorization_bundle_digest: str
    keyring_generation: int
    executor: SignatureEnvelopeV108

    def __post_init__(self) -> None:
        _validate_id(self.receipt_id, "receipt_id")
        for value, name in (
            (self.receipt_digest, "receipt_digest"),
            (self.command_digest, "command_digest"),
            (self.authorization_bundle_digest, "authorization_bundle_digest"),
        ):
            _validate_digest(value, name)
        if self.keyring_generation <= 0:
            raise SigningValidationErrorV108("keyring_generation must be positive")

    @property
    def payload_digest(self) -> str:
        return receipt_payload_digest_v108(
            receipt_digest=self.receipt_digest,
            command_digest=self.command_digest,
            authorization_bundle_digest=self.authorization_bundle_digest,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": 108,
            "receipt_id": self.receipt_id,
            "receipt_digest": self.receipt_digest,
            "command_digest": self.command_digest,
            "authorization_bundle_digest": self.authorization_bundle_digest,
            "payload_digest": self.payload_digest,
            "keyring_generation": self.keyring_generation,
            "executor": self.executor.to_payload(),
        }

    @property
    def authorization_digest(self) -> str:
        return digest_v108(self.to_payload())


def verify_receipt_authorization_v108(
    receipt: ReceiptAuthorizationV108,
    *,
    keyring: VerifiedKeyringV108,
    observed_at: datetime,
    replay_ledger: SignatureReplayLedgerV108 | None = None,
) -> SigningKeyDescriptorV108:
    if receipt.keyring_generation != keyring.generation:
        raise SignatureVerificationErrorV108("receipt keyring generation mismatch")
    descriptor = verify_envelope_v108(
        receipt.executor,
        keyring=keyring,
        expected_purpose=SigningPurposeV108.EXECUTOR_RECEIPT,
        expected_domain="astra.rollout.receipt.v108",
        expected_payload_digest=receipt.payload_digest,
        observed_at=observed_at,
    )
    if replay_ledger is not None:
        replay_ledger.consume_many((receipt.executor,))
    return descriptor
