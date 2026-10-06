from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.qualification.evidence_registry import (
    EvidenceLifecycleStatus,
    EvidenceStateProof,
    EvidenceVerificationDecision,
    EvidenceVerificationStatus,
    verify_state_proof,
)
from app.qualification.profile_binding import ProfileBoundQualificationManifest
from app.qualification.profile_registry import (
    QualificationProfile,
    QualificationProfileStateProof,
    QualificationProfileStatus,
    verify_profile_state_proof,
)
from app.qualification.qualification_manifest import QualificationManifest
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
from app.qualification.trust_checkpoint import (
    SignedQualificationTrustCheckpoint,
    verify_trust_checkpoint,
)

_SCHEMA_VERSION = "astra-portable-qualification-verification-v3"


class PortableVerificationFailureCodeV3(StrEnum):
    BUNDLE_INVALID = "BUNDLE_INVALID"
    KEYRING_REJECTED = "KEYRING_REJECTED"
    EVIDENCE_SIGNATURE_REJECTED = "EVIDENCE_SIGNATURE_REJECTED"
    EVIDENCE_STATE_PROOF_INVALID = "EVIDENCE_STATE_PROOF_INVALID"
    PROFILE_STATE_PROOF_INVALID = "PROFILE_STATE_PROOF_INVALID"
    TRANSPARENCY_PROOF_INVALID = "TRANSPARENCY_PROOF_INVALID"
    TRUST_CHECKPOINT_SIGNATURE_REJECTED = "TRUST_CHECKPOINT_SIGNATURE_REJECTED"
    LIFECYCLE_MISMATCH = "LIFECYCLE_MISMATCH"


class PortableQualificationVerificationErrorV3(ValueError):
    def __init__(
        self,
        code: PortableVerificationFailureCodeV3,
        detail: str,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PortableQualificationVerificationBundleV3:
    profile: QualificationProfile
    manifest: QualificationManifest
    binding: ProfileBoundQualificationManifest
    signed_evidence: SignedQualificationEvidence
    registry_decision: EvidenceVerificationDecision
    evidence_state_proof: EvidenceStateProof
    profile_state_proof: QualificationProfileStateProof
    transparency_entry: QualificationTransparencyEntry
    transparency_inclusion_proof: QualificationInclusionProof
    transparency_head: QualificationTransparencyTreeHead
    signed_trust_checkpoint: SignedQualificationTrustCheckpoint
    keyring_snapshot: QualificationKeyringSnapshot
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("portable qualification v3 bundle schema mismatch")
        self.profile.validate()
        self.manifest.validate()
        self.binding.validate()
        self.signed_evidence.validate()
        self.registry_decision.validate()
        self.evidence_state_proof.validate()
        self.profile_state_proof.validate()
        self.transparency_entry.validate()
        self.transparency_inclusion_proof.validate()
        self.transparency_head.validate()
        self.signed_trust_checkpoint.validate()
        self.keyring_snapshot.validate()

        if self.manifest.profile_id != self.profile.profile_id:
            raise ValueError("portable v3 profile_id mismatch")
        if self.manifest.profile_version != self.profile.version:
            raise ValueError("portable v3 profile_version mismatch")
        if self.manifest.scope != self.profile.scope:
            raise ValueError("portable v3 profile scope mismatch")
        if self.manifest.environment not in self.profile.allowed_environments:
            raise ValueError("portable v3 environment not allowed by profile")
        if _aware(self.manifest.issued_at, "manifest.issued_at") < _aware(
            self.profile.created_at,
            "profile.created_at",
        ):
            raise ValueError("portable v3 manifest predates profile")

        if self.binding.manifest_id != self.manifest.manifest_id:
            raise ValueError("portable v3 binding manifest id mismatch")
        if self.binding.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("portable v3 binding manifest digest mismatch")
        if self.binding.profile_id != self.profile.profile_id:
            raise ValueError("portable v3 binding profile id mismatch")
        if self.binding.profile_version != self.profile.version:
            raise ValueError("portable v3 binding profile version mismatch")
        if self.binding.profile_sha256 != self.profile.profile_sha256:
            raise ValueError("portable v3 binding profile digest mismatch")

        if self.signed_evidence.binding_id != self.binding.binding_id:
            raise ValueError("portable v3 signed binding id mismatch")
        if self.signed_evidence.binding_sha256 != self.binding.binding_sha256:
            raise ValueError("portable v3 signed binding digest mismatch")
        if self.signed_evidence.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("portable v3 signed manifest digest mismatch")
        if self.signed_evidence.profile_sha256 != self.profile.profile_sha256:
            raise ValueError("portable v3 signed profile digest mismatch")

        if self.registry_decision.evidence_id != self.signed_evidence.evidence_id:
            raise ValueError("portable v3 registry evidence id mismatch")
        if self.registry_decision.binding_sha256 != self.binding.binding_sha256:
            raise ValueError("portable v3 registry binding mismatch")
        if self.registry_decision.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("portable v3 registry manifest mismatch")
        if self.registry_decision.profile_sha256 != self.profile.profile_sha256:
            raise ValueError("portable v3 registry profile mismatch")

        if self.evidence_state_proof.evidence_id != self.signed_evidence.evidence_id:
            raise ValueError("portable v3 evidence state proof id mismatch")
        evidence_record = self.evidence_state_proof.record
        if evidence_record.binding_sha256 != self.binding.binding_sha256:
            raise ValueError("portable v3 evidence state binding mismatch")
        if evidence_record.manifest_sha256 != self.manifest.manifest_sha256:
            raise ValueError("portable v3 evidence state manifest mismatch")
        if evidence_record.profile_sha256 != self.profile.profile_sha256:
            raise ValueError("portable v3 evidence state profile mismatch")
        if (
            self.evidence_state_proof.state_root_sha256
            != self.registry_decision.state_root_sha256
        ):
            raise ValueError("portable v3 evidence registry state root mismatch")

        profile_record = self.profile_state_proof.record
        if self.profile_state_proof.profile_ref != self.profile.profile_ref:
            raise ValueError("portable v3 profile state ref mismatch")
        if profile_record.profile.profile_sha256 != self.profile.profile_sha256:
            raise ValueError("portable v3 profile state digest mismatch")

        if self.transparency_entry.object_id != self.signed_evidence.evidence_id:
            raise ValueError("portable v3 transparency object id mismatch")
        if (
            self.transparency_entry.object_sha256
            != self.signed_evidence.envelope.envelope_sha256
        ):
            raise ValueError("portable v3 transparency object digest mismatch")
        if self.transparency_inclusion_proof.leaf_sha256 != self.transparency_entry.leaf_sha256:
            raise ValueError("portable v3 transparency leaf mismatch")
        if self.transparency_inclusion_proof.root_sha256 != self.transparency_head.root_sha256:
            raise ValueError("portable v3 transparency proof root mismatch")
        if self.transparency_inclusion_proof.tree_size != self.transparency_head.tree_size:
            raise ValueError("portable v3 transparency proof size mismatch")

        checkpoint = self.signed_trust_checkpoint.checkpoint
        if checkpoint.evidence_event_head_sha256 != self.registry_decision.registry_head_sha256:
            raise ValueError("portable v3 trust evidence event head mismatch")
        if checkpoint.evidence_state_root_sha256 != self.evidence_state_proof.state_root_sha256:
            raise ValueError("portable v3 trust evidence state root mismatch")
        if checkpoint.profile_state_root_sha256 != self.profile_state_proof.state_root_sha256:
            raise ValueError("portable v3 trust profile state root mismatch")
        if checkpoint.transparency_root_sha256 != self.transparency_head.root_sha256:
            raise ValueError("portable v3 trust transparency root mismatch")
        if checkpoint.transparency_tree_size != self.transparency_head.tree_size:
            raise ValueError("portable v3 trust transparency size mismatch")
        if checkpoint.transparency_tree_head_sha256 != self.transparency_head.tree_head_sha256:
            raise ValueError("portable v3 trust transparency head mismatch")

        if (
            self.signed_evidence.envelope.keyring_generation
            != self.keyring_snapshot.generation
        ):
            raise ValueError("portable v3 evidence keyring generation mismatch")
        if (
            self.signed_trust_checkpoint.envelope.keyring_generation
            != self.keyring_snapshot.generation
        ):
            raise ValueError("portable v3 trust checkpoint keyring generation mismatch")

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
            "evidence_state_proof": {
                "evidence_id": self.evidence_state_proof.evidence_id,
                "record": self.evidence_state_proof.record.state_payload(),
                "leaf_index": self.evidence_state_proof.leaf_index,
                "tree_size": self.evidence_state_proof.tree_size,
                "audit_path": list(self.evidence_state_proof.audit_path),
                "state_root_sha256": self.evidence_state_proof.state_root_sha256,
            },
            "profile_state_proof": {
                "profile_ref": self.profile_state_proof.profile_ref,
                "record": self.profile_state_proof.record.state_payload(),
                "leaf_index": self.profile_state_proof.leaf_index,
                "tree_size": self.profile_state_proof.tree_size,
                "audit_path": list(self.profile_state_proof.audit_path),
                "state_root_sha256": self.profile_state_proof.state_root_sha256,
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
            "signed_trust_checkpoint": self.signed_trust_checkpoint.payload(),
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
        return f"qverifyv3_{self.bundle_sha256[:24]}"


@dataclass(frozen=True)
class PortableQualificationVerificationResultV3:
    bundle_id: str
    bundle_sha256: str
    evidence_id: str
    binding_id: str
    manifest_id: str
    profile_ref: str
    evidence_lifecycle_status: EvidenceVerificationStatus
    profile_lifecycle_status: QualificationProfileStatus
    usable: bool
    replacement_evidence_id: str | None
    evidence_signer_key_id: str
    checkpoint_signer_key_id: str
    keyring_generation: int
    evidence_registry_state_root_sha256: str
    profile_registry_state_root_sha256: str
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
            "evidence_lifecycle_status": self.evidence_lifecycle_status.value,
            "profile_lifecycle_status": self.profile_lifecycle_status.value,
            "usable": self.usable,
            "replacement_evidence_id": self.replacement_evidence_id,
            "evidence_signer_key_id": self.evidence_signer_key_id,
            "checkpoint_signer_key_id": self.checkpoint_signer_key_id,
            "keyring_generation": self.keyring_generation,
            "evidence_registry_state_root_sha256": self.evidence_registry_state_root_sha256,
            "profile_registry_state_root_sha256": self.profile_registry_state_root_sha256,
            "transparency_root_sha256": self.transparency_root_sha256,
            "transparency_tree_size": self.transparency_tree_size,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


def verify_portable_qualification_bundle_v3(
    *,
    bundle: PortableQualificationVerificationBundleV3,
    trusted_root_public_keys: Mapping[str, bytes],
    previous_keyring_generation: int,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> PortableQualificationVerificationResultV3:
    try:
        bundle.validate()
        now = _aware(observed_at, "observed_at")
    except ValueError as exc:
        raise PortableQualificationVerificationErrorV3(
            PortableVerificationFailureCodeV3.BUNDLE_INVALID,
            str(exc),
        ) from exc

    try:
        keyring = verify_qualification_keyring(
            bundle.keyring_snapshot,
            trusted_root_public_keys=trusted_root_public_keys,
            previous_generation=previous_keyring_generation,
            observed_at=now,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
    except ValueError as exc:
        raise PortableQualificationVerificationErrorV3(
            PortableVerificationFailureCodeV3.KEYRING_REJECTED,
            str(exc),
        ) from exc

    try:
        evidence = verify_qualification_evidence(
            binding=bundle.binding,
            signed_evidence=bundle.signed_evidence,
            keyring=keyring,
            observed_at=now,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
    except ValueError as exc:
        raise PortableQualificationVerificationErrorV3(
            PortableVerificationFailureCodeV3.EVIDENCE_SIGNATURE_REJECTED,
            str(exc),
        ) from exc

    if not verify_state_proof(bundle.evidence_state_proof):
        raise PortableQualificationVerificationErrorV3(
            PortableVerificationFailureCodeV3.EVIDENCE_STATE_PROOF_INVALID,
            "portable v3 evidence state proof is invalid",
        )
    if not verify_profile_state_proof(bundle.profile_state_proof):
        raise PortableQualificationVerificationErrorV3(
            PortableVerificationFailureCodeV3.PROFILE_STATE_PROOF_INVALID,
            "portable v3 profile state proof is invalid",
        )
    if not verify_inclusion_proof(bundle.transparency_inclusion_proof):
        raise PortableQualificationVerificationErrorV3(
            PortableVerificationFailureCodeV3.TRANSPARENCY_PROOF_INVALID,
            "portable v3 transparency proof is invalid",
        )

    try:
        checkpoint = verify_trust_checkpoint(
            signed_checkpoint=bundle.signed_trust_checkpoint,
            keyring=keyring,
            observed_at=now,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
    except ValueError as exc:
        raise PortableQualificationVerificationErrorV3(
            PortableVerificationFailureCodeV3.TRUST_CHECKPOINT_SIGNATURE_REJECTED,
            str(exc),
        ) from exc

    expected_evidence_status = {
        EvidenceLifecycleStatus.ACTIVE: EvidenceVerificationStatus.VALID,
        EvidenceLifecycleStatus.SUPERSEDED: EvidenceVerificationStatus.SUPERSEDED,
        EvidenceLifecycleStatus.REVOKED: EvidenceVerificationStatus.REVOKED,
    }[bundle.evidence_state_proof.record.status]
    lifecycle_mismatch = (
        bundle.registry_decision.status is not expected_evidence_status
        or evidence.evidence_id != bundle.registry_decision.evidence_id
        or checkpoint.evidence_state_root_sha256
        != bundle.evidence_state_proof.state_root_sha256
        or checkpoint.profile_state_root_sha256
        != bundle.profile_state_proof.state_root_sha256
        or checkpoint.transparency_root_sha256
        != bundle.transparency_inclusion_proof.root_sha256
    )
    if lifecycle_mismatch:
        raise PortableQualificationVerificationErrorV3(
            PortableVerificationFailureCodeV3.LIFECYCLE_MISMATCH,
            "portable v3 lifecycle decision or trust checkpoint binding mismatch",
        )

    profile_status = bundle.profile_state_proof.record.status
    usable = (
        bundle.registry_decision.status is EvidenceVerificationStatus.VALID
        and profile_status is QualificationProfileStatus.ACTIVE
    )
    return PortableQualificationVerificationResultV3(
        bundle_id=bundle.bundle_id,
        bundle_sha256=bundle.bundle_sha256,
        evidence_id=evidence.evidence_id,
        binding_id=evidence.binding_id,
        manifest_id=evidence.manifest_id,
        profile_ref=bundle.profile.profile_ref,
        evidence_lifecycle_status=bundle.registry_decision.status,
        profile_lifecycle_status=profile_status,
        usable=usable,
        replacement_evidence_id=bundle.registry_decision.replacement_evidence_id,
        evidence_signer_key_id=evidence.signer_key_id,
        checkpoint_signer_key_id=checkpoint.signer_key_id,
        keyring_generation=keyring.generation,
        evidence_registry_state_root_sha256=checkpoint.evidence_state_root_sha256,
        profile_registry_state_root_sha256=checkpoint.profile_state_root_sha256,
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
