from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.evidence_registry import QualificationEvidenceRegistry
from app.qualification.profile_registry import QualificationProfileRegistry
from app.qualification.signing_authority import (
    QualificationSignatureEnvelope,
    QualificationSignatureReplayLedger,
    QualificationSigningKeyDescriptor,
    QualificationSigningProvider,
    VerifiedQualificationKeyring,
    sign_qualification_payload,
    verify_qualification_signature,
)
from app.qualification.transparency_log import QualificationTransparencyTreeHead

_SCHEMA_VERSION = "astra-qualification-trust-checkpoint-v3"
_DOMAIN = "astra.qualification.trust-checkpoint.v3"
_GENESIS = "0" * 64


@dataclass(frozen=True)
class QualificationTrustCheckpoint:
    evidence_event_count: int
    evidence_event_head_sha256: str
    evidence_state_root_sha256: str
    profile_record_count: int
    profile_state_root_sha256: str
    transparency_tree_size: int
    transparency_root_sha256: str
    transparency_tree_head_sha256: str
    previous_trust_checkpoint_sha256: str
    issued_at: datetime
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("qualification trust checkpoint schema mismatch")
        if self.evidence_event_count < 0:
            raise ValueError("evidence_event_count must be non-negative")
        if self.profile_record_count < 0:
            raise ValueError("profile_record_count must be non-negative")
        if self.transparency_tree_size < 0:
            raise ValueError("transparency_tree_size must be non-negative")
        for name, value in (
            ("evidence_event_head_sha256", self.evidence_event_head_sha256),
            ("evidence_state_root_sha256", self.evidence_state_root_sha256),
            ("profile_state_root_sha256", self.profile_state_root_sha256),
            ("transparency_root_sha256", self.transparency_root_sha256),
            ("transparency_tree_head_sha256", self.transparency_tree_head_sha256),
            ("previous_trust_checkpoint_sha256", self.previous_trust_checkpoint_sha256),
        ):
            _digest(value, name)
        _aware(self.issued_at, "issued_at")
        if self.evidence_event_count == 0:
            if self.evidence_event_head_sha256 != _GENESIS:
                raise ValueError("empty evidence registry requires genesis event head")
            if self.evidence_state_root_sha256 != _GENESIS:
                raise ValueError("empty evidence registry requires genesis state root")
        if self.profile_record_count == 0 and self.profile_state_root_sha256 != _GENESIS:
            raise ValueError("empty profile registry requires genesis state root")
        if self.transparency_tree_size == 0 and self.transparency_root_sha256 != _GENESIS:
            raise ValueError("empty transparency log requires genesis root")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "evidence_event_count": self.evidence_event_count,
            "evidence_event_head_sha256": self.evidence_event_head_sha256,
            "evidence_state_root_sha256": self.evidence_state_root_sha256,
            "profile_record_count": self.profile_record_count,
            "profile_state_root_sha256": self.profile_state_root_sha256,
            "transparency_tree_size": self.transparency_tree_size,
            "transparency_root_sha256": self.transparency_root_sha256,
            "transparency_tree_head_sha256": self.transparency_tree_head_sha256,
            "previous_trust_checkpoint_sha256": self.previous_trust_checkpoint_sha256,
            "issued_at": _aware(self.issued_at, "issued_at").isoformat(),
        }

    @property
    def checkpoint_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def checkpoint_id(self) -> str:
        return f"qtrust_{self.checkpoint_sha256[:24]}"


@dataclass(frozen=True)
class SignedQualificationTrustCheckpoint:
    checkpoint: QualificationTrustCheckpoint
    envelope: QualificationSignatureEnvelope

    def validate(self) -> None:
        self.checkpoint.validate()
        self.envelope.validate()
        if self.envelope.domain != _DOMAIN:
            raise ValueError("qualification trust checkpoint domain mismatch")
        if self.envelope.payload_sha256 != self.checkpoint.checkpoint_sha256:
            raise ValueError("qualification trust checkpoint payload mismatch")

    @property
    def signed_checkpoint_id(self) -> str:
        self.validate()
        return f"qsignedtrust_{self.envelope.envelope_sha256[:24]}"

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "signed_checkpoint_id": self.signed_checkpoint_id,
            "checkpoint_id": self.checkpoint.checkpoint_id,
            "checkpoint": self.checkpoint.payload(),
            "checkpoint_sha256": self.checkpoint.checkpoint_sha256,
            "signature": {
                **self.envelope.unsigned_payload(),
                "signature_b64": self.envelope.signature_b64,
            },
            "signature_envelope_sha256": self.envelope.envelope_sha256,
        }


@dataclass(frozen=True)
class VerifiedQualificationTrustCheckpoint:
    checkpoint_id: str
    checkpoint_sha256: str
    evidence_event_count: int
    evidence_event_head_sha256: str
    evidence_state_root_sha256: str
    profile_record_count: int
    profile_state_root_sha256: str
    transparency_tree_size: int
    transparency_root_sha256: str
    transparency_tree_head_sha256: str
    previous_trust_checkpoint_sha256: str
    signer_key_id: str
    signer_owner_id: str
    signer_key_generation: int
    keyring_generation: int
    verified_at: datetime

    def payload(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_sha256": self.checkpoint_sha256,
            "evidence_event_count": self.evidence_event_count,
            "evidence_event_head_sha256": self.evidence_event_head_sha256,
            "evidence_state_root_sha256": self.evidence_state_root_sha256,
            "profile_record_count": self.profile_record_count,
            "profile_state_root_sha256": self.profile_state_root_sha256,
            "transparency_tree_size": self.transparency_tree_size,
            "transparency_root_sha256": self.transparency_root_sha256,
            "transparency_tree_head_sha256": self.transparency_tree_head_sha256,
            "previous_trust_checkpoint_sha256": self.previous_trust_checkpoint_sha256,
            "signer_key_id": self.signer_key_id,
            "signer_owner_id": self.signer_owner_id,
            "signer_key_generation": self.signer_key_generation,
            "keyring_generation": self.keyring_generation,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


def build_trust_checkpoint(
    *,
    evidence_registry: QualificationEvidenceRegistry,
    profile_registry: QualificationProfileRegistry,
    transparency_head: QualificationTransparencyTreeHead,
    issued_at: datetime,
    previous_checkpoint: QualificationTrustCheckpoint | None = None,
) -> QualificationTrustCheckpoint:
    transparency_head.validate()
    now = _aware(issued_at, "issued_at")
    if now < _aware(evidence_registry.latest_observed_at, "latest evidence event"):
        raise ValueError("trust checkpoint cannot predate evidence registry state")
    if now < _aware(profile_registry.latest_updated_at, "latest profile update"):
        raise ValueError("trust checkpoint cannot predate profile registry state")
    if now < _aware(transparency_head.issued_at, "transparency_head.issued_at"):
        raise ValueError("trust checkpoint cannot predate transparency head")

    previous_sha = (
        _GENESIS if previous_checkpoint is None else previous_checkpoint.checkpoint_sha256
    )
    if previous_checkpoint is not None:
        previous_checkpoint.validate()
        if now < _aware(previous_checkpoint.issued_at, "previous_checkpoint.issued_at"):
            raise ValueError("trust checkpoint time regression")
        if evidence_registry.event_count < previous_checkpoint.evidence_event_count:
            raise ValueError("trust checkpoint evidence event count regression")
        if profile_registry.record_count < previous_checkpoint.profile_record_count:
            raise ValueError("trust checkpoint profile record count regression")
        if transparency_head.tree_size < previous_checkpoint.transparency_tree_size:
            raise ValueError("trust checkpoint transparency size regression")

    checkpoint = QualificationTrustCheckpoint(
        evidence_event_count=evidence_registry.event_count,
        evidence_event_head_sha256=evidence_registry.verify_chain(),
        evidence_state_root_sha256=evidence_registry.state_root_sha256,
        profile_record_count=profile_registry.record_count,
        profile_state_root_sha256=profile_registry.state_root_sha256,
        transparency_tree_size=transparency_head.tree_size,
        transparency_root_sha256=transparency_head.root_sha256,
        transparency_tree_head_sha256=transparency_head.tree_head_sha256,
        previous_trust_checkpoint_sha256=previous_sha,
        issued_at=now,
    )
    checkpoint.validate()
    return checkpoint


def sign_trust_checkpoint(
    *,
    checkpoint: QualificationTrustCheckpoint,
    provider: QualificationSigningProvider,
    descriptor: QualificationSigningKeyDescriptor,
    keyring_generation: int,
    signature_id: str,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
    max_lifetime_seconds: int = 600,
) -> SignedQualificationTrustCheckpoint:
    checkpoint.validate()
    signature_time = _aware(issued_at, "issued_at")
    if signature_time < _aware(checkpoint.issued_at, "checkpoint.issued_at"):
        raise ValueError("trust checkpoint signature cannot predate checkpoint")
    envelope = sign_qualification_payload(
        provider=provider,
        descriptor=descriptor,
        keyring_generation=keyring_generation,
        signature_id=signature_id,
        domain=_DOMAIN,
        payload_sha256=checkpoint.checkpoint_sha256,
        issued_at=signature_time,
        expires_at=expires_at,
        nonce=nonce,
        max_lifetime_seconds=max_lifetime_seconds,
    )
    signed = SignedQualificationTrustCheckpoint(
        checkpoint=checkpoint,
        envelope=envelope,
    )
    signed.validate()
    return signed


def verify_trust_checkpoint(
    *,
    signed_checkpoint: SignedQualificationTrustCheckpoint,
    keyring: VerifiedQualificationKeyring,
    observed_at: datetime,
    replay_ledger: QualificationSignatureReplayLedger | None = None,
    max_clock_skew_seconds: int = 5,
) -> VerifiedQualificationTrustCheckpoint:
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
    return VerifiedQualificationTrustCheckpoint(
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_sha256=checkpoint.checkpoint_sha256,
        evidence_event_count=checkpoint.evidence_event_count,
        evidence_event_head_sha256=checkpoint.evidence_event_head_sha256,
        evidence_state_root_sha256=checkpoint.evidence_state_root_sha256,
        profile_record_count=checkpoint.profile_record_count,
        profile_state_root_sha256=checkpoint.profile_state_root_sha256,
        transparency_tree_size=checkpoint.transparency_tree_size,
        transparency_root_sha256=checkpoint.transparency_root_sha256,
        transparency_tree_head_sha256=checkpoint.transparency_tree_head_sha256,
        previous_trust_checkpoint_sha256=checkpoint.previous_trust_checkpoint_sha256,
        signer_key_id=descriptor.key_id,
        signer_owner_id=descriptor.owner_id,
        signer_key_generation=descriptor.generation,
        keyring_generation=keyring.generation,
        verified_at=_aware(observed_at, "observed_at"),
    )


def verify_trust_checkpoint_successor(
    *,
    previous: QualificationTrustCheckpoint,
    current: QualificationTrustCheckpoint,
) -> bool:
    previous.validate()
    current.validate()
    return (
        current.previous_trust_checkpoint_sha256 == previous.checkpoint_sha256
        and current.evidence_event_count >= previous.evidence_event_count
        and current.profile_record_count >= previous.profile_record_count
        and current.transparency_tree_size >= previous.transparency_tree_size
        and _aware(current.issued_at, "current.issued_at")
        >= _aware(previous.issued_at, "previous.issued_at")
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
