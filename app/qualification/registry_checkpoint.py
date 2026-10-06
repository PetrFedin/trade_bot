from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.evidence_registry import QualificationEvidenceRegistry
from app.qualification.signing_authority import (
    QualificationSignatureEnvelope,
    QualificationSignatureReplayLedger,
    QualificationSigningKeyDescriptor,
    QualificationSigningProvider,
    VerifiedQualificationKeyring,
    sign_qualification_payload,
    verify_qualification_signature,
)

_SCHEMA_VERSION = "astra-profile-bound-registry-checkpoint-v1"
_DOMAIN = "astra.qualification.profile-bound-registry-checkpoint.v1"
_GENESIS = "0" * 64


@dataclass(frozen=True)
class QualificationRegistryCheckpoint:
    event_count: int
    registry_head_sha256: str
    issued_at: datetime
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("qualification registry checkpoint schema mismatch")
        if self.event_count < 0:
            raise ValueError("qualification registry checkpoint event_count must be non-negative")
        _digest(self.registry_head_sha256, "registry_head_sha256")
        _aware(self.issued_at, "issued_at")
        if self.event_count == 0 and self.registry_head_sha256 != _GENESIS:
            raise ValueError("empty qualification registry requires genesis head")
        if self.event_count > 0 and self.registry_head_sha256 == _GENESIS:
            raise ValueError("non-empty qualification registry cannot use genesis head")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "event_count": self.event_count,
            "registry_head_sha256": self.registry_head_sha256,
            "issued_at": _aware(self.issued_at, "issued_at").isoformat(),
        }

    @property
    def checkpoint_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def checkpoint_id(self) -> str:
        return f"qcheckpoint_{self.checkpoint_sha256[:24]}"


@dataclass(frozen=True)
class SignedQualificationRegistryCheckpoint:
    checkpoint: QualificationRegistryCheckpoint
    envelope: QualificationSignatureEnvelope

    def validate(self) -> None:
        self.checkpoint.validate()
        if self.envelope.domain != _DOMAIN:
            raise ValueError("qualification registry checkpoint domain mismatch")
        if self.envelope.payload_sha256 != self.checkpoint.checkpoint_sha256:
            raise ValueError("qualification registry checkpoint payload mismatch")

    @property
    def signed_checkpoint_id(self) -> str:
        self.validate()
        return f"qsignedcheckpoint_{self.envelope.envelope_sha256[:24]}"

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "signed_checkpoint_id": self.signed_checkpoint_id,
            "checkpoint_id": self.checkpoint.checkpoint_id,
            "checkpoint_sha256": self.checkpoint.checkpoint_sha256,
            "checkpoint": self.checkpoint.payload(),
            "signature": {
                **self.envelope.unsigned_payload(),
                "signature_b64": self.envelope.signature_b64,
            },
            "signature_envelope_sha256": self.envelope.envelope_sha256,
        }


@dataclass(frozen=True)
class VerifiedQualificationRegistryCheckpoint:
    signed_checkpoint_id: str
    checkpoint_id: str
    checkpoint_sha256: str
    event_count: int
    registry_head_sha256: str
    signer_key_id: str
    signer_owner_id: str
    signer_key_generation: int
    keyring_generation: int
    verified_at: datetime

    def payload(self) -> dict[str, object]:
        return {
            "signed_checkpoint_id": self.signed_checkpoint_id,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_sha256": self.checkpoint_sha256,
            "event_count": self.event_count,
            "registry_head_sha256": self.registry_head_sha256,
            "signer_key_id": self.signer_key_id,
            "signer_owner_id": self.signer_owner_id,
            "signer_key_generation": self.signer_key_generation,
            "keyring_generation": self.keyring_generation,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


def build_registry_checkpoint(
    *,
    registry: QualificationEvidenceRegistry,
    issued_at: datetime,
) -> QualificationRegistryCheckpoint:
    head = registry.verify_chain()
    checkpoint = QualificationRegistryCheckpoint(
        event_count=registry.event_count,
        registry_head_sha256=head,
        issued_at=issued_at,
    )
    checkpoint.validate()
    return checkpoint


def sign_registry_checkpoint(
    *,
    checkpoint: QualificationRegistryCheckpoint,
    provider: QualificationSigningProvider,
    descriptor: QualificationSigningKeyDescriptor,
    keyring_generation: int,
    signature_id: str,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
    max_lifetime_seconds: int = 600,
) -> SignedQualificationRegistryCheckpoint:
    checkpoint.validate()
    issued = _aware(issued_at, "issued_at")
    if issued < _aware(checkpoint.issued_at, "checkpoint.issued_at"):
        raise ValueError("registry checkpoint signature cannot predate checkpoint")
    envelope = sign_qualification_payload(
        provider=provider,
        descriptor=descriptor,
        keyring_generation=keyring_generation,
        signature_id=signature_id,
        domain=_DOMAIN,
        payload_sha256=checkpoint.checkpoint_sha256,
        issued_at=issued,
        expires_at=expires_at,
        nonce=nonce,
        max_lifetime_seconds=max_lifetime_seconds,
    )
    signed = SignedQualificationRegistryCheckpoint(
        checkpoint=checkpoint,
        envelope=envelope,
    )
    signed.validate()
    return signed


def verify_registry_checkpoint(
    *,
    signed_checkpoint: SignedQualificationRegistryCheckpoint,
    keyring: VerifiedQualificationKeyring,
    observed_at: datetime,
    replay_ledger: QualificationSignatureReplayLedger | None = None,
    max_clock_skew_seconds: int = 5,
) -> VerifiedQualificationRegistryCheckpoint:
    signed_checkpoint.validate()
    descriptor = verify_qualification_signature(
        signed_checkpoint.envelope,
        keyring=keyring,
        expected_domain=_DOMAIN,
        expected_payload_sha256=signed_checkpoint.checkpoint.checkpoint_sha256,
        observed_at=observed_at,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    if replay_ledger is not None:
        replay_ledger.consume(signed_checkpoint.envelope)
    checkpoint = signed_checkpoint.checkpoint
    return VerifiedQualificationRegistryCheckpoint(
        signed_checkpoint_id=signed_checkpoint.signed_checkpoint_id,
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_sha256=checkpoint.checkpoint_sha256,
        event_count=checkpoint.event_count,
        registry_head_sha256=checkpoint.registry_head_sha256,
        signer_key_id=descriptor.key_id,
        signer_owner_id=descriptor.owner_id,
        signer_key_generation=descriptor.generation,
        keyring_generation=keyring.generation,
        verified_at=_aware(observed_at, "observed_at"),
    )


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized
