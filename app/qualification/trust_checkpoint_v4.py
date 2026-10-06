from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.evidence_registry import QualificationEvidenceRegistry
from app.qualification.profile_registry import QualificationProfileRegistry
from app.qualification.profile_transparency import (
    QualificationProfilePublicationReceipt,
    verify_profile_publication_receipt,
)
from app.qualification.signing_authority import (
    QualificationSignatureEnvelope,
    QualificationSignatureReplayLedger,
    QualificationSigningKeyDescriptor,
    QualificationSigningProvider,
    VerifiedQualificationKeyring,
    sign_qualification_payload,
    verify_qualification_signature,
)
from app.qualification.transparency_log import QualificationTransparencyLog

_SCHEMA_VERSION = "astra-qualification-trust-checkpoint-v4"
_DOMAIN = "astra.qualification.trust-checkpoint.v4"
_GENESIS = "0" * 64


@dataclass(frozen=True)
class QualificationTrustCheckpointV4:
    evidence_event_count: int
    evidence_event_head_sha256: str
    evidence_state_root_sha256: str
    profile_record_count: int
    profile_state_root_sha256: str
    profile_event_count: int
    profile_event_head_sha256: str
    profile_publication_receipt_sha256: str
    profile_publication_tree_size: int
    profile_publication_root_sha256: str
    profile_publication_tree_head_sha256: str
    transparency_tree_size: int
    transparency_root_sha256: str
    transparency_tree_head_sha256: str
    previous_trust_checkpoint_sha256: str
    issued_at: datetime
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("qualification trust checkpoint v4 schema mismatch")
        for name, value in (
            ("evidence_event_count", self.evidence_event_count),
            ("profile_record_count", self.profile_record_count),
            ("profile_event_count", self.profile_event_count),
            ("profile_publication_tree_size", self.profile_publication_tree_size),
            ("transparency_tree_size", self.transparency_tree_size),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        for name, value in (
            ("evidence_event_head_sha256", self.evidence_event_head_sha256),
            ("evidence_state_root_sha256", self.evidence_state_root_sha256),
            ("profile_state_root_sha256", self.profile_state_root_sha256),
            ("profile_event_head_sha256", self.profile_event_head_sha256),
            (
                "profile_publication_receipt_sha256",
                self.profile_publication_receipt_sha256,
            ),
            (
                "profile_publication_root_sha256",
                self.profile_publication_root_sha256,
            ),
            (
                "profile_publication_tree_head_sha256",
                self.profile_publication_tree_head_sha256,
            ),
            ("transparency_root_sha256", self.transparency_root_sha256),
            ("transparency_tree_head_sha256", self.transparency_tree_head_sha256),
            (
                "previous_trust_checkpoint_sha256",
                self.previous_trust_checkpoint_sha256,
            ),
        ):
            _digest(value, name)
        _aware(self.issued_at, "issued_at")

        if self.evidence_event_count == 0:
            if self.evidence_event_head_sha256 != _GENESIS:
                raise ValueError("empty evidence journal requires genesis head")
            if self.evidence_state_root_sha256 != _GENESIS:
                raise ValueError("empty evidence registry requires genesis state root")
        if self.profile_record_count == 0 and self.profile_state_root_sha256 != _GENESIS:
            raise ValueError("empty profile registry requires genesis state root")
        if self.profile_event_count == 0 and self.profile_event_head_sha256 != _GENESIS:
            raise ValueError("empty profile event journal requires genesis head")
        if self.profile_publication_tree_size > self.transparency_tree_size:
            raise ValueError("profile publication tree cannot exceed transparency tree")
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
            "profile_event_count": self.profile_event_count,
            "profile_event_head_sha256": self.profile_event_head_sha256,
            "profile_publication_receipt_sha256": (
                self.profile_publication_receipt_sha256
            ),
            "profile_publication_tree_size": self.profile_publication_tree_size,
            "profile_publication_root_sha256": self.profile_publication_root_sha256,
            "profile_publication_tree_head_sha256": (
                self.profile_publication_tree_head_sha256
            ),
            "transparency_tree_size": self.transparency_tree_size,
            "transparency_root_sha256": self.transparency_root_sha256,
            "transparency_tree_head_sha256": self.transparency_tree_head_sha256,
            "previous_trust_checkpoint_sha256": (
                self.previous_trust_checkpoint_sha256
            ),
            "issued_at": _aware(self.issued_at, "issued_at").isoformat(),
        }

    @property
    def checkpoint_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def checkpoint_id(self) -> str:
        return f"qtrustv4_{self.checkpoint_sha256[:24]}"


@dataclass(frozen=True)
class SignedQualificationTrustCheckpointV4:
    checkpoint: QualificationTrustCheckpointV4
    envelope: QualificationSignatureEnvelope

    def validate(self) -> None:
        self.checkpoint.validate()
        self.envelope.validate()
        if self.envelope.domain != _DOMAIN:
            raise ValueError("qualification trust checkpoint v4 domain mismatch")
        if self.envelope.payload_sha256 != self.checkpoint.checkpoint_sha256:
            raise ValueError("qualification trust checkpoint v4 payload mismatch")

    @property
    def signed_checkpoint_id(self) -> str:
        self.validate()
        return f"qsignedtrustv4_{self.envelope.envelope_sha256[:24]}"

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
class VerifiedQualificationTrustCheckpointV4:
    checkpoint_id: str
    checkpoint_sha256: str
    evidence_event_count: int
    evidence_event_head_sha256: str
    evidence_state_root_sha256: str
    profile_record_count: int
    profile_state_root_sha256: str
    profile_event_count: int
    profile_event_head_sha256: str
    profile_publication_receipt_sha256: str
    profile_publication_tree_size: int
    profile_publication_root_sha256: str
    profile_publication_tree_head_sha256: str
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
            "profile_event_count": self.profile_event_count,
            "profile_event_head_sha256": self.profile_event_head_sha256,
            "profile_publication_receipt_sha256": (
                self.profile_publication_receipt_sha256
            ),
            "profile_publication_tree_size": self.profile_publication_tree_size,
            "profile_publication_root_sha256": self.profile_publication_root_sha256,
            "profile_publication_tree_head_sha256": (
                self.profile_publication_tree_head_sha256
            ),
            "transparency_tree_size": self.transparency_tree_size,
            "transparency_root_sha256": self.transparency_root_sha256,
            "transparency_tree_head_sha256": self.transparency_tree_head_sha256,
            "previous_trust_checkpoint_sha256": (
                self.previous_trust_checkpoint_sha256
            ),
            "signer_key_id": self.signer_key_id,
            "signer_owner_id": self.signer_owner_id,
            "signer_key_generation": self.signer_key_generation,
            "keyring_generation": self.keyring_generation,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


def build_trust_checkpoint_v4(
    *,
    evidence_registry: QualificationEvidenceRegistry,
    profile_registry: QualificationProfileRegistry,
    profile_publication_receipt: QualificationProfilePublicationReceipt,
    transparency_log: QualificationTransparencyLog,
    issued_at: datetime,
    previous_checkpoint: QualificationTrustCheckpointV4 | None = None,
) -> QualificationTrustCheckpointV4:
    now = _aware(issued_at, "issued_at")
    profile_publication_receipt.validate()

    if not verify_profile_publication_receipt(
        receipt=profile_publication_receipt,
        profile_registry=profile_registry,
        transparency_log=transparency_log,
    ):
        raise ValueError("profile lifecycle publication receipt is not current and valid")

    if now < _aware(evidence_registry.latest_observed_at, "latest evidence event"):
        raise ValueError("trust checkpoint v4 cannot predate evidence registry state")
    if now < _aware(profile_registry.latest_updated_at, "latest profile update"):
        raise ValueError("trust checkpoint v4 cannot predate profile registry state")
    if now < _aware(profile_publication_receipt.observed_at, "publication receipt"):
        raise ValueError("trust checkpoint v4 cannot predate profile publication receipt")

    latest_transparency = transparency_log.latest_head()
    if now < _aware(latest_transparency.issued_at, "transparency head"):
        raise ValueError("trust checkpoint v4 cannot predate transparency head")

    profile_event_head = profile_registry.verify_event_chain()
    if profile_event_head != profile_publication_receipt.profile_event_head_sha256:
        raise ValueError("profile publication receipt event head mismatch")
    if profile_registry.event_count != profile_publication_receipt.profile_event_count:
        raise ValueError("profile publication receipt event count mismatch")

    previous_sha = (
        _GENESIS if previous_checkpoint is None else previous_checkpoint.checkpoint_sha256
    )
    if previous_checkpoint is not None:
        previous_checkpoint.validate()
        if now < _aware(previous_checkpoint.issued_at, "previous_checkpoint.issued_at"):
            raise ValueError("trust checkpoint v4 time regression")
        if evidence_registry.event_count < previous_checkpoint.evidence_event_count:
            raise ValueError("trust checkpoint v4 evidence event count regression")
        if profile_registry.record_count < previous_checkpoint.profile_record_count:
            raise ValueError("trust checkpoint v4 profile record count regression")
        if profile_registry.event_count < previous_checkpoint.profile_event_count:
            raise ValueError("trust checkpoint v4 profile event count regression")
        if latest_transparency.tree_size < previous_checkpoint.transparency_tree_size:
            raise ValueError("trust checkpoint v4 transparency size regression")

    checkpoint = QualificationTrustCheckpointV4(
        evidence_event_count=evidence_registry.event_count,
        evidence_event_head_sha256=evidence_registry.verify_chain(),
        evidence_state_root_sha256=evidence_registry.state_root_sha256,
        profile_record_count=profile_registry.record_count,
        profile_state_root_sha256=profile_registry.state_root_sha256,
        profile_event_count=profile_registry.event_count,
        profile_event_head_sha256=profile_event_head,
        profile_publication_receipt_sha256=(
            profile_publication_receipt.receipt_sha256
        ),
        profile_publication_tree_size=(
            profile_publication_receipt.transparency_tree_size
        ),
        profile_publication_root_sha256=(
            profile_publication_receipt.transparency_root_sha256
        ),
        profile_publication_tree_head_sha256=(
            profile_publication_receipt.transparency_tree_head_sha256
        ),
        transparency_tree_size=latest_transparency.tree_size,
        transparency_root_sha256=latest_transparency.root_sha256,
        transparency_tree_head_sha256=latest_transparency.tree_head_sha256,
        previous_trust_checkpoint_sha256=previous_sha,
        issued_at=now,
    )
    checkpoint.validate()
    return checkpoint


def sign_trust_checkpoint_v4(
    *,
    checkpoint: QualificationTrustCheckpointV4,
    provider: QualificationSigningProvider,
    descriptor: QualificationSigningKeyDescriptor,
    keyring_generation: int,
    signature_id: str,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
    max_lifetime_seconds: int = 600,
) -> SignedQualificationTrustCheckpointV4:
    checkpoint.validate()
    signature_time = _aware(issued_at, "issued_at")
    if signature_time < _aware(checkpoint.issued_at, "checkpoint.issued_at"):
        raise ValueError("trust checkpoint v4 signature cannot predate checkpoint")
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
    signed = SignedQualificationTrustCheckpointV4(
        checkpoint=checkpoint,
        envelope=envelope,
    )
    signed.validate()
    return signed


def verify_trust_checkpoint_v4(
    *,
    signed_checkpoint: SignedQualificationTrustCheckpointV4,
    keyring: VerifiedQualificationKeyring,
    observed_at: datetime,
    replay_ledger: QualificationSignatureReplayLedger | None = None,
    max_clock_skew_seconds: int = 5,
) -> VerifiedQualificationTrustCheckpointV4:
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
    return VerifiedQualificationTrustCheckpointV4(
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_sha256=checkpoint.checkpoint_sha256,
        evidence_event_count=checkpoint.evidence_event_count,
        evidence_event_head_sha256=checkpoint.evidence_event_head_sha256,
        evidence_state_root_sha256=checkpoint.evidence_state_root_sha256,
        profile_record_count=checkpoint.profile_record_count,
        profile_state_root_sha256=checkpoint.profile_state_root_sha256,
        profile_event_count=checkpoint.profile_event_count,
        profile_event_head_sha256=checkpoint.profile_event_head_sha256,
        profile_publication_receipt_sha256=(
            checkpoint.profile_publication_receipt_sha256
        ),
        profile_publication_tree_size=checkpoint.profile_publication_tree_size,
        profile_publication_root_sha256=checkpoint.profile_publication_root_sha256,
        profile_publication_tree_head_sha256=(
            checkpoint.profile_publication_tree_head_sha256
        ),
        transparency_tree_size=checkpoint.transparency_tree_size,
        transparency_root_sha256=checkpoint.transparency_root_sha256,
        transparency_tree_head_sha256=checkpoint.transparency_tree_head_sha256,
        previous_trust_checkpoint_sha256=(
            checkpoint.previous_trust_checkpoint_sha256
        ),
        signer_key_id=descriptor.key_id,
        signer_owner_id=descriptor.owner_id,
        signer_key_generation=descriptor.generation,
        keyring_generation=keyring.generation,
        verified_at=_aware(observed_at, "observed_at"),
    )


def verify_trust_checkpoint_v4_successor(
    *,
    previous: QualificationTrustCheckpointV4,
    current: QualificationTrustCheckpointV4,
) -> bool:
    previous.validate()
    current.validate()
    return (
        current.previous_trust_checkpoint_sha256 == previous.checkpoint_sha256
        and current.evidence_event_count >= previous.evidence_event_count
        and current.profile_record_count >= previous.profile_record_count
        and current.profile_event_count >= previous.profile_event_count
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
