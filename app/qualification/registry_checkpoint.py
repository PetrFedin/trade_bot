from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.evidence_registry import InMemoryQualificationEvidenceRegistry
from app.runtime.signing_authority_v108 import (
    Ed25519SigningProviderV108,
    SignatureEnvelopeV108,
    SignatureReplayLedgerV108,
    SigningKeyDescriptorV108,
    SigningPurposeV108,
    VerifiedKeyringV108,
    sign_envelope_v108,
    verify_envelope_v108,
)

_SCHEMA_VERSION = "astra-qualification-registry-checkpoint-v1"
_DOMAIN = "astra.qualification.registry-checkpoint.v1"


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
        if self.event_count == 0 and self.registry_head_sha256 != "0" * 64:
            raise ValueError("empty qualification registry requires genesis head")

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
    envelope: SignatureEnvelopeV108

    def validate(self) -> None:
        self.checkpoint.validate()
        if self.envelope.purpose is not SigningPurposeV108.QUALIFICATION_EVIDENCE:
            raise ValueError("qualification registry checkpoint purpose mismatch")
        if self.envelope.domain != _DOMAIN:
            raise ValueError("qualification registry checkpoint domain mismatch")
        if self.envelope.payload_digest != self.checkpoint.checkpoint_sha256:
            raise ValueError("qualification registry checkpoint payload mismatch")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "checkpoint_id": self.checkpoint.checkpoint_id,
            "checkpoint": self.checkpoint.payload(),
            "checkpoint_sha256": self.checkpoint.checkpoint_sha256,
            "signature": self.envelope.to_payload(),
            "signature_envelope_sha256": self.envelope.envelope_digest,
        }


@dataclass(frozen=True)
class VerifiedQualificationRegistryCheckpoint:
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
    registry: InMemoryQualificationEvidenceRegistry,
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
    provider: Ed25519SigningProviderV108,
    descriptor: SigningKeyDescriptorV108,
    keyring_generation: int,
    signature_id: str,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
    max_lifetime_seconds: int = 600,
) -> SignedQualificationRegistryCheckpoint:
    checkpoint.validate()
    if _aware(issued_at, "issued_at") < _aware(checkpoint.issued_at, "checkpoint.issued_at"):
        raise ValueError("registry checkpoint signature cannot predate checkpoint")
    envelope = sign_envelope_v108(
        provider=provider,
        descriptor=descriptor,
        keyring_generation=keyring_generation,
        signature_id=signature_id,
        purpose=SigningPurposeV108.QUALIFICATION_EVIDENCE,
        domain=_DOMAIN,
        payload_digest=checkpoint.checkpoint_sha256,
        issued_at=issued_at,
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
    keyring: VerifiedKeyringV108,
    observed_at: datetime,
    replay_ledger: SignatureReplayLedgerV108 | None = None,
    max_clock_skew_seconds: int = 5,
) -> VerifiedQualificationRegistryCheckpoint:
    signed_checkpoint.validate()
    descriptor = verify_envelope_v108(
        signed_checkpoint.envelope,
        keyring=keyring,
        expected_purpose=SigningPurposeV108.QUALIFICATION_EVIDENCE,
        expected_domain=_DOMAIN,
        expected_payload_digest=signed_checkpoint.checkpoint.checkpoint_sha256,
        observed_at=observed_at,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    if replay_ledger is not None:
        replay_ledger.consume_many((signed_checkpoint.envelope,))
    checkpoint = signed_checkpoint.checkpoint
    return VerifiedQualificationRegistryCheckpoint(
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
