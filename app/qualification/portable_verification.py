from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.evidence_registry import (
    EvidenceVerificationDecision,
    EvidenceVerificationStatus,
)
from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.registry_checkpoint import (
    SignedQualificationRegistryCheckpoint,
    VerifiedQualificationRegistryCheckpoint,
    verify_registry_checkpoint,
)
from app.qualification.signed_evidence import (
    SignedQualificationEvidence,
    VerifiedQualificationEvidence,
    verify_qualification_evidence,
)
from app.runtime.signing_authority_v108 import VerifiedKeyringV108

_SCHEMA_VERSION = "astra-portable-qualification-verification-v1"


@dataclass(frozen=True)
class PortableQualificationVerificationBundle:
    manifest: QualificationManifest
    signed_evidence: SignedQualificationEvidence
    lifecycle: EvidenceVerificationDecision
    signed_registry_checkpoint: SignedQualificationRegistryCheckpoint
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("portable qualification verification schema mismatch")
        self.manifest.validate()
        self.signed_evidence.validate()
        self.lifecycle.validate()
        self.signed_registry_checkpoint.validate()

        if self.signed_evidence.manifest_id != self.manifest.manifest_id:
            raise ValueError("portable qualification manifest id mismatch")
        if self.signed_evidence.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("portable qualification manifest digest mismatch")
        if self.lifecycle.evidence_id != self.signed_evidence.evidence_id:
            raise ValueError("portable qualification evidence id mismatch")
        if self.lifecycle.status in {
            EvidenceVerificationStatus.UNKNOWN,
            EvidenceVerificationStatus.INVALID,
        }:
            raise ValueError("portable qualification bundle requires registered evidence")
        if self.lifecycle.manifest_id != self.manifest.manifest_id:
            raise ValueError("portable qualification lifecycle manifest id mismatch")
        if self.lifecycle.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("portable qualification lifecycle manifest digest mismatch")
        if (
            self.lifecycle.registry_head_sha256
            != self.signed_registry_checkpoint.checkpoint.registry_head_sha256
        ):
            raise ValueError("portable qualification registry head mismatch")

    @property
    def bundle_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def bundle_id(self) -> str:
        return f"qverify_{self.bundle_sha256[:24]}"

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "manifest": self.manifest.payload(),
            "signed_evidence": self.signed_evidence.payload(),
            "lifecycle": self.lifecycle.payload(),
            "signed_registry_checkpoint": self.signed_registry_checkpoint.payload(),
        }


@dataclass(frozen=True)
class PortableQualificationVerificationResult:
    bundle_id: str
    bundle_sha256: str
    evidence_id: str
    manifest_id: str
    manifest_sha256: str
    lifecycle_status: EvidenceVerificationStatus
    replacement_evidence_id: str | None
    evidence_signer_key_id: str
    evidence_signer_owner_id: str
    registry_signer_key_id: str
    registry_signer_owner_id: str
    registry_head_sha256: str
    registry_event_count: int
    keyring_generation: int
    verified_at: datetime

    def payload(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "evidence_id": self.evidence_id,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "lifecycle_status": self.lifecycle_status.value,
            "replacement_evidence_id": self.replacement_evidence_id,
            "evidence_signer_key_id": self.evidence_signer_key_id,
            "evidence_signer_owner_id": self.evidence_signer_owner_id,
            "registry_signer_key_id": self.registry_signer_key_id,
            "registry_signer_owner_id": self.registry_signer_owner_id,
            "registry_head_sha256": self.registry_head_sha256,
            "registry_event_count": self.registry_event_count,
            "keyring_generation": self.keyring_generation,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


def verify_portable_qualification_bundle(
    *,
    bundle: PortableQualificationVerificationBundle,
    keyring: VerifiedKeyringV108,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> PortableQualificationVerificationResult:
    """Verify a portable qualification artifact without trusting its transport."""

    bundle.validate()
    evidence_verification: VerifiedQualificationEvidence = verify_qualification_evidence(
        manifest=bundle.manifest,
        signed_evidence=bundle.signed_evidence,
        keyring=keyring,
        observed_at=observed_at,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    registry_verification: VerifiedQualificationRegistryCheckpoint = (
        verify_registry_checkpoint(
            signed_checkpoint=bundle.signed_registry_checkpoint,
            keyring=keyring,
            observed_at=observed_at,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
    )

    if evidence_verification.manifest_sha256 != bundle.manifest.manifest_sha256:
        raise ValueError("portable qualification verified manifest digest mismatch")
    if (
        registry_verification.registry_head_sha256
        != bundle.lifecycle.registry_head_sha256
    ):
        raise ValueError("portable qualification verified registry head mismatch")
    if registry_verification.keyring_generation != evidence_verification.keyring_generation:
        raise ValueError("portable qualification keyring generation mismatch")

    return PortableQualificationVerificationResult(
        bundle_id=bundle.bundle_id,
        bundle_sha256=bundle.bundle_sha256,
        evidence_id=bundle.signed_evidence.evidence_id,
        manifest_id=bundle.manifest.manifest_id,
        manifest_sha256=bundle.manifest.manifest_sha256,
        lifecycle_status=bundle.lifecycle.status,
        replacement_evidence_id=bundle.lifecycle.replacement_evidence_id,
        evidence_signer_key_id=evidence_verification.signer_key_id,
        evidence_signer_owner_id=evidence_verification.signer_owner_id,
        registry_signer_key_id=registry_verification.signer_key_id,
        registry_signer_owner_id=registry_verification.signer_owner_id,
        registry_head_sha256=registry_verification.registry_head_sha256,
        registry_event_count=registry_verification.event_count,
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
