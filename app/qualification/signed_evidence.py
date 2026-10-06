from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.profile_binding import ProfileBoundQualificationManifest
from app.qualification.signing_authority import (
    QualificationSignatureEnvelope,
    QualificationSignatureReplayLedger,
    QualificationSigningKeyDescriptor,
    QualificationSigningProvider,
    VerifiedQualificationKeyring,
    sign_qualification_payload,
    verify_qualification_signature,
)

_SCHEMA_VERSION = "astra-profile-bound-signed-qualification-evidence-v1"
_DOMAIN = "astra.qualification.profile-bound-evidence.v1"


@dataclass(frozen=True)
class SignedQualificationEvidence:
    binding_id: str
    binding_sha256: str
    manifest_id: str
    manifest_sha256: str
    profile_sha256: str
    envelope: QualificationSignatureEnvelope
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("signed qualification evidence schema mismatch")
        if not self.binding_id.startswith("qbinding_"):
            raise ValueError("signed qualification evidence requires binding id")
        if not self.manifest_id.startswith("qmanifest_"):
            raise ValueError("signed qualification evidence requires manifest id")
        for name, value in (
            ("binding_sha256", self.binding_sha256),
            ("manifest_sha256", self.manifest_sha256),
            ("profile_sha256", self.profile_sha256),
        ):
            _digest(value, name)
        if self.envelope.domain != _DOMAIN:
            raise ValueError("signed qualification evidence domain mismatch")
        if self.envelope.payload_sha256 != self.binding_sha256:
            raise ValueError("signed qualification evidence payload mismatch")

    @property
    def evidence_id(self) -> str:
        self.validate()
        return f"qevidence_{self.envelope.envelope_sha256[:24]}"

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "binding_id": self.binding_id,
            "binding_sha256": self.binding_sha256,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "profile_sha256": self.profile_sha256,
            "signature": {
                **self.envelope.unsigned_payload(),
                "signature_b64": self.envelope.signature_b64,
            },
            "signature_envelope_sha256": self.envelope.envelope_sha256,
        }


@dataclass(frozen=True)
class VerifiedQualificationEvidence:
    evidence_id: str
    binding_id: str
    binding_sha256: str
    manifest_id: str
    manifest_sha256: str
    profile_sha256: str
    signer_key_id: str
    signer_owner_id: str
    signer_key_generation: int
    keyring_generation: int
    verified_at: datetime

    def payload(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "binding_id": self.binding_id,
            "binding_sha256": self.binding_sha256,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "profile_sha256": self.profile_sha256,
            "signer_key_id": self.signer_key_id,
            "signer_owner_id": self.signer_owner_id,
            "signer_key_generation": self.signer_key_generation,
            "keyring_generation": self.keyring_generation,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


def sign_qualification_binding(
    *,
    binding: ProfileBoundQualificationManifest,
    provider: QualificationSigningProvider,
    descriptor: QualificationSigningKeyDescriptor,
    keyring_generation: int,
    signature_id: str,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
    max_lifetime_seconds: int = 600,
) -> SignedQualificationEvidence:
    binding.validate()
    envelope = sign_qualification_payload(
        provider=provider,
        descriptor=descriptor,
        keyring_generation=keyring_generation,
        signature_id=signature_id,
        domain=_DOMAIN,
        payload_sha256=binding.binding_sha256,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
        max_lifetime_seconds=max_lifetime_seconds,
    )
    signed = SignedQualificationEvidence(
        binding_id=binding.binding_id,
        binding_sha256=binding.binding_sha256,
        manifest_id=binding.manifest_id,
        manifest_sha256=binding.manifest_sha256,
        profile_sha256=binding.profile_sha256,
        envelope=envelope,
    )
    signed.validate()
    return signed


def verify_qualification_evidence(
    *,
    binding: ProfileBoundQualificationManifest,
    signed_evidence: SignedQualificationEvidence,
    keyring: VerifiedQualificationKeyring,
    observed_at: datetime,
    replay_ledger: QualificationSignatureReplayLedger | None = None,
    max_clock_skew_seconds: int = 5,
) -> VerifiedQualificationEvidence:
    binding.validate()
    signed_evidence.validate()
    if signed_evidence.binding_id != binding.binding_id:
        raise ValueError("qualification evidence binding id mismatch")
    if signed_evidence.binding_sha256 != binding.binding_sha256:
        raise ValueError("qualification evidence binding digest mismatch")
    if signed_evidence.manifest_id != binding.manifest_id:
        raise ValueError("qualification evidence manifest id mismatch")
    if signed_evidence.manifest_sha256 != binding.manifest_sha256:
        raise ValueError("qualification evidence manifest digest mismatch")
    if signed_evidence.profile_sha256 != binding.profile_sha256:
        raise ValueError("qualification evidence profile digest mismatch")

    descriptor = verify_qualification_signature(
        signed_evidence.envelope,
        keyring=keyring,
        expected_domain=_DOMAIN,
        expected_payload_sha256=binding.binding_sha256,
        observed_at=observed_at,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    if replay_ledger is not None:
        replay_ledger.consume(signed_evidence.envelope)

    return VerifiedQualificationEvidence(
        evidence_id=signed_evidence.evidence_id,
        binding_id=binding.binding_id,
        binding_sha256=binding.binding_sha256,
        manifest_id=binding.manifest_id,
        manifest_sha256=binding.manifest_sha256,
        profile_sha256=binding.profile_sha256,
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


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized
