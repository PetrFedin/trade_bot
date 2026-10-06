from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.signing_authority import (
    QualificationSignatureEnvelope,
    QualificationSignatureReplayLedger,
    QualificationSigningKeyDescriptor,
    QualificationSigningProvider,
    VerifiedQualificationKeyring,
    sign_qualification_payload,
    verify_qualification_signature,
)
_SCHEMA_VERSION = "astra-signed-qualification-evidence-v1"
_DOMAIN = "astra.qualification.evidence.v1"


@dataclass(frozen=True)
class SignedQualificationEvidence:
    manifest_id: str
    manifest_sha256: str
    envelope: QualificationSignatureEnvelope
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("signed qualification evidence schema mismatch")
        if not self.manifest_id.startswith("qmanifest_"):
            raise ValueError("signed qualification evidence requires a manifest id")
        _digest(self.manifest_sha256, "manifest_sha256")
        if self.envelope.domain != _DOMAIN:
            raise ValueError("signed qualification evidence domain mismatch")
        if self.envelope.payload_sha256 != self.manifest_sha256:
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
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "signature": {
                **self.envelope.unsigned_payload(),
                "signature_b64": self.envelope.signature_b64,
            },
            "signature_envelope_sha256": self.envelope.envelope_sha256,
        }


@dataclass(frozen=True)
class VerifiedQualificationEvidence:
    evidence_id: str
    manifest_id: str
    manifest_sha256: str
    signer_key_id: str
    signer_owner_id: str
    signer_key_generation: int
    keyring_generation: int
    verified_at: datetime

    def payload(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "signer_key_id": self.signer_key_id,
            "signer_owner_id": self.signer_owner_id,
            "signer_key_generation": self.signer_key_generation,
            "keyring_generation": self.keyring_generation,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


def sign_qualification_manifest(
    *,
    manifest: QualificationManifest,
    provider: QualificationSigningProvider,
    descriptor: QualificationSigningKeyDescriptor,
    keyring_generation: int,
    signature_id: str,
    issued_at: datetime,
    expires_at: datetime,
    nonce: str,
    max_lifetime_seconds: int = 600,
) -> SignedQualificationEvidence:
    manifest.validate()
    envelope = sign_qualification_payload(
        provider=provider,
        descriptor=descriptor,
        keyring_generation=keyring_generation,
        signature_id=signature_id,
        domain=_DOMAIN,
        payload_sha256=manifest.manifest_sha256,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
        max_lifetime_seconds=max_lifetime_seconds,
    )
    signed = SignedQualificationEvidence(
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.manifest_sha256,
        envelope=envelope,
    )
    signed.validate()
    return signed


def verify_qualification_evidence(
    *,
    manifest: QualificationManifest,
    signed_evidence: SignedQualificationEvidence,
    keyring: VerifiedQualificationKeyring,
    observed_at: datetime,
    replay_ledger: QualificationSignatureReplayLedger | None = None,
    max_clock_skew_seconds: int = 5,
) -> VerifiedQualificationEvidence:
    manifest.validate()
    signed_evidence.validate()
    if signed_evidence.manifest_id != manifest.manifest_id:
        raise ValueError("qualification evidence manifest id mismatch")
    if signed_evidence.manifest_sha256 != manifest.manifest_sha256:
        raise ValueError("qualification evidence manifest digest mismatch")

    descriptor = verify_qualification_signature(
        signed_evidence.envelope,
        keyring=keyring,
        expected_domain=_DOMAIN,
        expected_payload_sha256=manifest.manifest_sha256,
        observed_at=observed_at,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    if replay_ledger is not None:
        replay_ledger.consume(signed_evidence.envelope)

    return VerifiedQualificationEvidence(
        evidence_id=signed_evidence.evidence_id,
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.manifest_sha256,
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
