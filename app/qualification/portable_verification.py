from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.evidence_registry import (
    EvidenceLifecycleStatus,
    EvidenceStateProof,
    EvidenceVerificationDecision,
    EvidenceVerificationStatus,
    verify_state_proof,
)
from app.qualification.profile_binding import ProfileBoundQualificationManifest
from app.qualification.profile_registry import QualificationProfile
from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.registry_checkpoint import (
    SignedQualificationRegistryCheckpoint,
    verify_registry_checkpoint,
)
from app.qualification.signed_evidence import (
    SignedQualificationEvidence,
    verify_qualification_evidence,
)
from app.qualification.signing_authority import (
    QualificationKeyringSnapshot,
    verify_qualification_keyring,
)
from app.qualification.transparency_log import (
    QualificationInclusionProof,
    QualificationTransparencyEntry,
    QualificationTransparencyTreeHead,
    verify_inclusion_proof,
)

_SCHEMA_VERSION = "astra-portable-qualification-verification-v2"


@dataclass(frozen=True)
class PortableQualificationVerificationBundle:
    profile: QualificationProfile
    manifest: QualificationManifest
    binding: ProfileBoundQualificationManifest
    signed_evidence: SignedQualificationEvidence
    registry_decision: EvidenceVerificationDecision
    registry_state_proof: EvidenceStateProof
    transparency_entry: QualificationTransparencyEntry
    transparency_inclusion_proof: QualificationInclusionProof
    transparency_head: QualificationTransparencyTreeHead
    signed_registry_checkpoint: SignedQualificationRegistryCheckpoint
    keyring_snapshot: QualificationKeyringSnapshot
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("portable qualification bundle schema mismatch")
        self.profile.validate()
        self.manifest.validate()
        self.binding.validate()
        self.signed_evidence.validate()
        self.registry_decision.validate()
        self.registry_state_proof.validate()
        self.transparency_entry.validate()
        self.transparency_inclusion_proof.validate()
        self.transparency_head.validate()
        self.signed_registry_checkpoint.validate()
        self.keyring_snapshot.validate()

        if self.manifest.profile_id != self.profile.profile_id:
            raise ValueError("portable bundle profile_id mismatch")
        if self.manifest.profile_version != self.profile.version:
            raise ValueError("portable bundle profile_version mismatch")
        if self.manifest.scope != self.profile.scope:
            raise ValueError("portable bundle profile scope mismatch")
        if self.manifest.environment not in self.profile.allowed_environments:
            raise ValueError("portable bundle environment not allowed by profile")
        if _aware(self.manifest.issued_at, "manifest.issued_at") < _aware(
            self.profile.created_at,
            "profile.created_at",
        ):
            raise ValueError("portable bundle manifest predates profile")

        if self.binding.manifest_id != self.manifest.manifest_id:
            raise ValueError("portable bundle binding manifest id mismatch")
        if self.binding.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("portable bundle binding manifest digest mismatch")
        if self.binding.profile_id != self.profile.profile_id:
            raise ValueError("portable bundle binding profile_id mismatch")
        if self.binding.profile_version != self.profile.version:
            raise ValueError("portable bundle binding profile_version mismatch")
        if self.binding.profile_sha256 != self.profile.profile_sha256:
            raise ValueError("portable bundle binding profile digest mismatch")
        if self.binding.scope != self.profile.scope:
            raise ValueError("portable bundle binding scope mismatch")

        if self.signed_evidence.binding_id != self.binding.binding_id:
            raise ValueError("portable bundle signed binding id mismatch")
        if self.signed_evidence.binding_sha256 != self.binding.binding_sha256:
            raise ValueError("portable bundle signed binding digest mismatch")
        if self.signed_evidence.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("portable bundle signed manifest digest mismatch")
        if self.signed_evidence.profile_sha256 != self.profile.profile_sha256:
            raise ValueError("portable bundle signed profile digest mismatch")

        if self.registry_decision.evidence_id != self.signed_evidence.evidence_id:
            raise ValueError("portable bundle registry evidence id mismatch")
        if self.registry_decision.binding_sha256 != self.binding.binding_sha256:
            raise ValueError("portable bundle registry binding mismatch")
        if self.registry_decision.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("portable bundle registry manifest mismatch")
        if self.registry_decision.profile_sha256 != self.profile.profile_sha256:
            raise ValueError("portable bundle registry profile mismatch")

        if self.registry_state_proof.evidence_id != self.signed_evidence.evidence_id:
            raise ValueError("portable bundle state proof evidence id mismatch")
        record = self.registry_state_proof.record
        if record.binding_sha256 != self.binding.binding_sha256:
            raise ValueError("portable bundle state proof binding mismatch")
        if record.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("portable bundle state proof manifest mismatch")
        if record.profile_sha256 != self.profile.profile_sha256:
            raise ValueError("portable bundle state proof profile mismatch")
        if (
            self.registry_state_proof.state_root_sha256
            != self.registry_decision.state_root_sha256
        ):
            raise ValueError("portable bundle registry state root mismatch")

        checkpoint = self.signed_registry_checkpoint.checkpoint
        if (
            checkpoint.registry_event_head_sha256
            != self.registry_decision.registry_head_sha256
        ):
            raise ValueError("portable bundle registry event head mismatch")
        if checkpoint.registry_state_root_sha256 != self.registry_decision.state_root_sha256:
            raise ValueError("portable bundle checkpoint state root mismatch")

        if self.transparency_entry.object_id != self.signed_evidence.evidence_id:
            raise ValueError("portable bundle transparency object id mismatch")
        if (
            self.transparency_entry.object_sha256
            != self.signed_evidence.envelope.envelope_sha256
        ):
            raise ValueError("portable bundle transparency object digest mismatch")
        if self.transparency_inclusion_proof.leaf_sha256 != self.transparency_entry.leaf_sha256:
            raise ValueError("portable bundle transparency leaf mismatch")
        if self.transparency_inclusion_proof.root_sha256 != self.transparency_head.root_sha256:
            raise ValueError("portable bundle transparency proof root mismatch")
        if self.transparency_inclusion_proof.tree_size != self.transparency_head.tree_size:
            raise ValueError("portable bundle transparency proof size mismatch")
        if checkpoint.transparency_root_sha256 != self.transparency_head.root_sha256:
            raise ValueError("portable bundle checkpoint transparency root mismatch")
        if checkpoint.transparency_tree_size != self.transparency_head.tree_size:
            raise ValueError("portable bundle checkpoint transparency size mismatch")
        if (
            checkpoint.transparency_tree_head_sha256
            != self.transparency_head.tree_head_sha256
        ):
            raise ValueError("portable bundle checkpoint transparency head mismatch")

        if (
            self.signed_evidence.envelope.keyring_generation
            != self.keyring_snapshot.generation
        ):
            raise ValueError("portable bundle evidence keyring generation mismatch")
        if (
            self.signed_registry_checkpoint.envelope.keyring_generation
            != self.keyring_snapshot.generation
        ):
            raise ValueError("portable bundle checkpoint keyring generation mismatch")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "profile": self.profile.payload(),
            "profile_sha256": self.profile.profile_sha256,
            "manifest": self.manifest.payload(),
            "binding": self.binding.payload(),
            "binding_sha256": self.binding.binding_sha256,
            "signed_evidence": self.signed_evidence.payload(),
            "registry_decision": self.registry_decision.payload(),
            "registry_state_proof": {
                "evidence_id": self.registry_state_proof.evidence_id,
                "record": self.registry_state_proof.record.state_payload(),
                "leaf_index": self.registry_state_proof.leaf_index,
                "tree_size": self.registry_state_proof.tree_size,
                "audit_path": list(self.registry_state_proof.audit_path),
                "state_root_sha256": self.registry_state_proof.state_root_sha256,
            },
            "transparency_entry": self.transparency_entry.payload(),
            "transparency_inclusion_proof": {
                "tree_size": self.transparency_inclusion_proof.tree_size,
                "leaf_index": self.transparency_inclusion_proof.leaf_index,
                "leaf_sha256": self.transparency_inclusion_proof.leaf_sha256,
                "audit_path": list(self.transparency_inclusion_proof.audit_path),
                "root_sha256": self.transparency_inclusion_proof.root_sha256,
            },
            "transparency_head": self.transparency_head.payload(),
            "signed_registry_checkpoint": self.signed_registry_checkpoint.payload(),
            "keyring_snapshot": {
                **self.keyring_snapshot.unsigned_payload(),
                "root_signature_b64": self.keyring_snapshot.root_signature_b64,
                "snapshot_sha256": self.keyring_snapshot.snapshot_sha256,
            },
        }

    @property
    def bundle_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def bundle_id(self) -> str:
        return f"qverify_{self.bundle_sha256[:24]}"


@dataclass(frozen=True)
class PortableQualificationVerificationResult:
    bundle_id: str
    bundle_sha256: str
    evidence_id: str
    binding_id: str
    manifest_id: str
    profile_ref: str
    lifecycle_status: EvidenceVerificationStatus
    replacement_evidence_id: str | None
    evidence_signer_key_id: str
    checkpoint_signer_key_id: str
    keyring_generation: int
    registry_event_head_sha256: str
    registry_state_root_sha256: str
    transparency_root_sha256: str
    transparency_tree_size: int
    verified_at: datetime

    def payload(self) -> dict[str, object]:
        return {
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "evidence_id": self.evidence_id,
            "binding_id": self.binding_id,
            "manifest_id": self.manifest_id,
            "profile_ref": self.profile_ref,
            "lifecycle_status": self.lifecycle_status.value,
            "replacement_evidence_id": self.replacement_evidence_id,
            "evidence_signer_key_id": self.evidence_signer_key_id,
            "checkpoint_signer_key_id": self.checkpoint_signer_key_id,
            "keyring_generation": self.keyring_generation,
            "registry_event_head_sha256": self.registry_event_head_sha256,
            "registry_state_root_sha256": self.registry_state_root_sha256,
            "transparency_root_sha256": self.transparency_root_sha256,
            "transparency_tree_size": self.transparency_tree_size,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


def verify_portable_qualification_bundle(
    *,
    bundle: PortableQualificationVerificationBundle,
    trusted_root_public_keys: Mapping[str, bytes],
    previous_keyring_generation: int,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> PortableQualificationVerificationResult:
    """Verify the qualification artifact from external trust roots only."""

    bundle.validate()
    now = _aware(observed_at, "observed_at")
    keyring = verify_qualification_keyring(
        bundle.keyring_snapshot,
        trusted_root_public_keys=trusted_root_public_keys,
        previous_generation=previous_keyring_generation,
        observed_at=now,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    evidence = verify_qualification_evidence(
        binding=bundle.binding,
        signed_evidence=bundle.signed_evidence,
        keyring=keyring,
        observed_at=now,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    checkpoint = verify_registry_checkpoint(
        signed_checkpoint=bundle.signed_registry_checkpoint,
        keyring=keyring,
        observed_at=now,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )

    if not verify_state_proof(bundle.registry_state_proof):
        raise ValueError("portable bundle registry state proof is invalid")
    if not verify_inclusion_proof(bundle.transparency_inclusion_proof):
        raise ValueError("portable bundle transparency inclusion proof is invalid")

    expected_status = {
        EvidenceLifecycleStatus.ACTIVE: EvidenceVerificationStatus.VALID,
        EvidenceLifecycleStatus.SUPERSEDED: EvidenceVerificationStatus.SUPERSEDED,
        EvidenceLifecycleStatus.REVOKED: EvidenceVerificationStatus.REVOKED,
    }[bundle.registry_state_proof.record.status]
    if bundle.registry_decision.status is not expected_status:
        raise ValueError("portable bundle lifecycle decision mismatch")
    if evidence.evidence_id != bundle.registry_decision.evidence_id:
        raise ValueError("portable bundle verified evidence identity mismatch")
    if checkpoint.registry_state_root_sha256 != bundle.registry_state_proof.state_root_sha256:
        raise ValueError("portable bundle verified checkpoint state mismatch")
    if checkpoint.transparency_root_sha256 != bundle.transparency_inclusion_proof.root_sha256:
        raise ValueError("portable bundle verified transparency root mismatch")

    return PortableQualificationVerificationResult(
        bundle_id=bundle.bundle_id,
        bundle_sha256=bundle.bundle_sha256,
        evidence_id=evidence.evidence_id,
        binding_id=evidence.binding_id,
        manifest_id=evidence.manifest_id,
        profile_ref=bundle.profile.profile_ref,
        lifecycle_status=bundle.registry_decision.status,
        replacement_evidence_id=bundle.registry_decision.replacement_evidence_id,
        evidence_signer_key_id=evidence.signer_key_id,
        checkpoint_signer_key_id=checkpoint.signer_key_id,
        keyring_generation=keyring.generation,
        registry_event_head_sha256=checkpoint.registry_event_head_sha256,
        registry_state_root_sha256=checkpoint.registry_state_root_sha256,
        transparency_root_sha256=checkpoint.transparency_root_sha256,
        transparency_tree_size=checkpoint.transparency_tree_size,
        verified_at=now,
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
