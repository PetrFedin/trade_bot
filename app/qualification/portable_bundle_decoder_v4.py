from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

from app.qualification.evidence_registry import (
    EvidenceLifecycleStatus,
    EvidenceRegistryRecord,
    EvidenceStateProof,
    EvidenceVerificationDecision,
    EvidenceVerificationStatus,
)
from app.qualification.portable_artifact_codec import (
    DecodedQualificationPortableArtifact,
    canonical_json_bytes,
)
from app.qualification.portable_verification_v3 import (
    PortableQualificationVerificationBundleV3,
)
from app.qualification.portable_verification_v4 import (
    PortableQualificationVerificationBundleV4,
)
from app.qualification.profile_binding import ProfileBoundQualificationManifest
from app.qualification.profile_event_delta import QualificationProfileEventDeltaProof
from app.qualification.profile_registry import (
    QualificationProfile,
    QualificationProfileEvent,
    QualificationProfileEventType,
    QualificationProfileRecord,
    QualificationProfileStateProof,
    QualificationProfileStatus,
)
from app.qualification.profile_transparency import (
    QualificationProfilePublicationReceipt,
)
from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.signed_evidence import SignedQualificationEvidence
from app.qualification.signing_authority import (
    QualificationKeyringSnapshot,
    QualificationSignatureEnvelope,
    QualificationSigningKeyDescriptor,
)
from app.qualification.transparency_log import (
    QualificationDeltaConsistencyProof,
    QualificationInclusionProof,
    QualificationTransparencyEntry,
    QualificationTransparencyTreeHead,
)
from app.qualification.trust_checkpoint import (
    QualificationTrustCheckpoint,
    SignedQualificationTrustCheckpoint,
)
from app.qualification.trust_checkpoint_v4 import (
    QualificationTrustCheckpointV4,
    SignedQualificationTrustCheckpointV4,
)


class QualificationBundleDecodeError(ValueError):
    """Fail-closed error raised while reconstructing a typed portable bundle."""


_E = TypeVar("_E", bound=StrEnum)


def decode_typed_portable_qualification_bundle_v4(
    artifact: DecodedQualificationPortableArtifact,
) -> PortableQualificationVerificationBundleV4:
    """Reconstruct a v4 bundle only after portable-artifact integrity validation."""

    artifact.validate()
    try:
        bundle = _decode_bundle_v4(artifact.bundle_payload, "$.bundle")
        bundle.validate()
    except QualificationBundleDecodeError:
        raise
    except (TypeError, ValueError) as exc:
        raise QualificationBundleDecodeError(
            f"typed portable bundle validation failed: {exc}"
        ) from exc

    if canonical_json_bytes(bundle.payload()) != canonical_json_bytes(
        artifact.bundle_payload
    ):
        raise QualificationBundleDecodeError(
            "typed portable bundle canonical roundtrip mismatch"
        )
    if bundle.bundle_sha256 != artifact.bundle_sha256:
        raise QualificationBundleDecodeError(
            "typed portable bundle digest does not match portable artifact"
        )
    if bundle.bundle_id != artifact.bundle_id:
        raise QualificationBundleDecodeError(
            "typed portable bundle id does not match portable artifact"
        )
    return bundle


def _decode_bundle_v4(
    value: object,
    path: str,
) -> PortableQualificationVerificationBundleV4:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "base_v3",
            "profile_event_delta",
            "profile_event_entries",
            "profile_event_inclusion_proofs",
            "profile_publication_receipt",
            "profile_publication_head",
            "transparency_consistency_proof",
            "current_transparency_head",
            "signed_trust_checkpoint_v4",
        },
    )
    bundle = PortableQualificationVerificationBundleV4(
        base_v3=_decode_bundle_v3(payload["base_v3"], f"{path}.base_v3"),
        profile_event_delta=_decode_profile_event_delta(
            payload["profile_event_delta"],
            f"{path}.profile_event_delta",
        ),
        profile_event_entries=tuple(
            _decode_transparency_entry(item, f"{path}.profile_event_entries[{index}]")
            for index, item in enumerate(
                _array(payload["profile_event_entries"], f"{path}.profile_event_entries")
            )
        ),
        profile_event_inclusion_proofs=tuple(
            _decode_inclusion_proof(
                item,
                f"{path}.profile_event_inclusion_proofs[{index}]",
            )
            for index, item in enumerate(
                _array(
                    payload["profile_event_inclusion_proofs"],
                    f"{path}.profile_event_inclusion_proofs",
                )
            )
        ),
        profile_publication_receipt=_decode_profile_publication_receipt(
            payload["profile_publication_receipt"],
            f"{path}.profile_publication_receipt",
        ),
        profile_publication_head=_decode_transparency_head(
            payload["profile_publication_head"],
            f"{path}.profile_publication_head",
        ),
        transparency_consistency_proof=_decode_consistency_proof(
            payload["transparency_consistency_proof"],
            f"{path}.transparency_consistency_proof",
        ),
        current_transparency_head=_decode_transparency_head(
            payload["current_transparency_head"],
            f"{path}.current_transparency_head",
        ),
        signed_trust_checkpoint_v4=_decode_signed_checkpoint_v4(
            payload["signed_trust_checkpoint_v4"],
            f"{path}.signed_trust_checkpoint_v4",
        ),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )
    return bundle


def _decode_bundle_v3(
    value: object,
    path: str,
) -> PortableQualificationVerificationBundleV3:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "profile",
            "profile_sha256",
            "manifest",
            "binding",
            "binding_sha256",
            "signed_evidence",
            "registry_decision",
            "evidence_state_proof",
            "profile_state_proof",
            "transparency_entry",
            "transparency_inclusion_proof",
            "transparency_head",
            "signed_trust_checkpoint",
            "keyring_snapshot",
        },
    )
    profile = _decode_profile(payload["profile"], f"{path}.profile")
    bundle = PortableQualificationVerificationBundleV3(
        profile=profile,
        manifest=_decode_manifest(payload["manifest"], f"{path}.manifest"),
        binding=_decode_binding(payload["binding"], f"{path}.binding"),
        signed_evidence=_decode_signed_evidence(
            payload["signed_evidence"],
            f"{path}.signed_evidence",
        ),
        registry_decision=_decode_registry_decision(
            payload["registry_decision"],
            f"{path}.registry_decision",
        ),
        evidence_state_proof=_decode_evidence_state_proof(
            payload["evidence_state_proof"],
            f"{path}.evidence_state_proof",
        ),
        profile_state_proof=_decode_profile_state_proof(
            payload["profile_state_proof"],
            profile=profile,
            path=f"{path}.profile_state_proof",
        ),
        transparency_entry=_decode_transparency_entry(
            payload["transparency_entry"],
            f"{path}.transparency_entry",
        ),
        transparency_inclusion_proof=_decode_inclusion_proof(
            payload["transparency_inclusion_proof"],
            f"{path}.transparency_inclusion_proof",
        ),
        transparency_head=_decode_transparency_head(
            payload["transparency_head"],
            f"{path}.transparency_head",
        ),
        signed_trust_checkpoint=_decode_signed_checkpoint_v3(
            payload["signed_trust_checkpoint"],
            f"{path}.signed_trust_checkpoint",
        ),
        keyring_snapshot=_decode_keyring_snapshot(
            payload["keyring_snapshot"],
            f"{path}.keyring_snapshot",
        ),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )
    return bundle


def _decode_profile(value: object, path: str) -> QualificationProfile:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "profile_id",
            "version",
            "scope",
            "allowed_environments",
            "required_corpus_classes",
            "required_checks",
            "required_assertions",
            "required_limitations",
            "created_at",
        },
    )
    return QualificationProfile(
        profile_id=_string(payload["profile_id"], f"{path}.profile_id"),
        version=_string(payload["version"], f"{path}.version"),
        scope=_string(payload["scope"], f"{path}.scope"),
        allowed_environments=_tuple_strings(
            payload["allowed_environments"],
            f"{path}.allowed_environments",
        ),
        required_corpus_classes=_tuple_strings(
            payload["required_corpus_classes"],
            f"{path}.required_corpus_classes",
        ),
        required_checks=_tuple_strings(
            payload["required_checks"],
            f"{path}.required_checks",
        ),
        required_assertions=_tuple_strings(
            payload["required_assertions"],
            f"{path}.required_assertions",
        ),
        required_limitations=_tuple_strings(
            payload["required_limitations"],
            f"{path}.required_limitations",
        ),
        created_at=_datetime(payload["created_at"], f"{path}.created_at"),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_manifest(value: object, path: str) -> QualificationManifest:
    payload = _object(
        value,
        path,
        {
            "manifest_id",
            "schema_version",
            "job_id",
            "job_result_sha256",
            "request_sha256",
            "organisation_id",
            "subject",
            "subject_version",
            "profile_id",
            "profile_version",
            "environment",
            "corpus_ids",
            "provider_replay_sha256",
            "adapter_conformance_sha256",
            "marketdata_integrity_sha256",
            "continuity_checkpoint_id",
            "provider",
            "venue",
            "symbol",
            "interval_seconds",
            "scope",
            "assertions",
            "limitations",
            "issued_at",
            "manifest_sha256",
        },
    )
    return QualificationManifest(
        job_id=_string(payload["job_id"], f"{path}.job_id"),
        job_result_sha256=_string(
            payload["job_result_sha256"],
            f"{path}.job_result_sha256",
        ),
        request_sha256=_string(payload["request_sha256"], f"{path}.request_sha256"),
        organisation_id=_string(
            payload["organisation_id"],
            f"{path}.organisation_id",
        ),
        subject=_string(payload["subject"], f"{path}.subject"),
        subject_version=_string(
            payload["subject_version"],
            f"{path}.subject_version",
        ),
        profile_id=_string(payload["profile_id"], f"{path}.profile_id"),
        profile_version=_string(
            payload["profile_version"],
            f"{path}.profile_version",
        ),
        environment=_string(payload["environment"], f"{path}.environment"),
        corpus_ids=_tuple_strings(payload["corpus_ids"], f"{path}.corpus_ids"),
        provider_replay_sha256=_string(
            payload["provider_replay_sha256"],
            f"{path}.provider_replay_sha256",
        ),
        adapter_conformance_sha256=_string(
            payload["adapter_conformance_sha256"],
            f"{path}.adapter_conformance_sha256",
        ),
        marketdata_integrity_sha256=_string(
            payload["marketdata_integrity_sha256"],
            f"{path}.marketdata_integrity_sha256",
        ),
        continuity_checkpoint_id=_string(
            payload["continuity_checkpoint_id"],
            f"{path}.continuity_checkpoint_id",
        ),
        provider=_string(payload["provider"], f"{path}.provider"),
        venue=_string(payload["venue"], f"{path}.venue"),
        symbol=_string(payload["symbol"], f"{path}.symbol"),
        interval_seconds=_integer(
            payload["interval_seconds"],
            f"{path}.interval_seconds",
        ),
        scope=_string(payload["scope"], f"{path}.scope"),
        assertions=_tuple_strings(payload["assertions"], f"{path}.assertions"),
        limitations=_tuple_strings(payload["limitations"], f"{path}.limitations"),
        issued_at=_datetime(payload["issued_at"], f"{path}.issued_at"),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_binding(
    value: object,
    path: str,
) -> ProfileBoundQualificationManifest:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "manifest_id",
            "manifest_sha256",
            "profile_id",
            "profile_version",
            "profile_sha256",
            "scope",
            "environment",
        },
    )
    return ProfileBoundQualificationManifest(
        manifest_id=_string(payload["manifest_id"], f"{path}.manifest_id"),
        manifest_sha256=_string(
            payload["manifest_sha256"],
            f"{path}.manifest_sha256",
        ),
        profile_id=_string(payload["profile_id"], f"{path}.profile_id"),
        profile_version=_string(
            payload["profile_version"],
            f"{path}.profile_version",
        ),
        profile_sha256=_string(
            payload["profile_sha256"],
            f"{path}.profile_sha256",
        ),
        scope=_string(payload["scope"], f"{path}.scope"),
        environment=_string(payload["environment"], f"{path}.environment"),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_signature_envelope(
    value: object,
    path: str,
) -> QualificationSignatureEnvelope:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "signature_id",
            "domain",
            "payload_sha256",
            "key_id",
            "key_generation",
            "keyring_generation",
            "issued_at",
            "expires_at",
            "nonce",
            "signature_b64",
        },
    )
    return QualificationSignatureEnvelope(
        signature_id=_string(payload["signature_id"], f"{path}.signature_id"),
        domain=_string(payload["domain"], f"{path}.domain"),
        payload_sha256=_string(
            payload["payload_sha256"],
            f"{path}.payload_sha256",
        ),
        key_id=_string(payload["key_id"], f"{path}.key_id"),
        key_generation=_integer(
            payload["key_generation"],
            f"{path}.key_generation",
        ),
        keyring_generation=_integer(
            payload["keyring_generation"],
            f"{path}.keyring_generation",
        ),
        issued_at=_datetime(payload["issued_at"], f"{path}.issued_at"),
        expires_at=_datetime(payload["expires_at"], f"{path}.expires_at"),
        nonce=_string(payload["nonce"], f"{path}.nonce"),
        signature_b64=_string(
            payload["signature_b64"],
            f"{path}.signature_b64",
        ),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_signed_evidence(
    value: object,
    path: str,
) -> SignedQualificationEvidence:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "evidence_id",
            "binding_id",
            "binding_sha256",
            "manifest_id",
            "manifest_sha256",
            "profile_sha256",
            "signature",
            "signature_envelope_sha256",
        },
    )
    return SignedQualificationEvidence(
        binding_id=_string(payload["binding_id"], f"{path}.binding_id"),
        binding_sha256=_string(
            payload["binding_sha256"],
            f"{path}.binding_sha256",
        ),
        manifest_id=_string(payload["manifest_id"], f"{path}.manifest_id"),
        manifest_sha256=_string(
            payload["manifest_sha256"],
            f"{path}.manifest_sha256",
        ),
        profile_sha256=_string(
            payload["profile_sha256"],
            f"{path}.profile_sha256",
        ),
        envelope=_decode_signature_envelope(
            payload["signature"],
            f"{path}.signature",
        ),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_registry_decision(
    value: object,
    path: str,
) -> EvidenceVerificationDecision:
    payload = _object(
        value,
        path,
        {
            "evidence_id",
            "status",
            "binding_id",
            "binding_sha256",
            "manifest_id",
            "manifest_sha256",
            "profile_sha256",
            "signer_key_id",
            "signer_owner_id",
            "signer_key_generation",
            "keyring_generation",
            "signature_envelope_sha256",
            "replacement_evidence_id",
            "reason",
            "registry_head_sha256",
            "state_root_sha256",
            "verified_at",
        },
    )
    return EvidenceVerificationDecision(
        evidence_id=_string(payload["evidence_id"], f"{path}.evidence_id"),
        status=_enum(
            EvidenceVerificationStatus,
            payload["status"],
            f"{path}.status",
        ),
        binding_id=_optional_string(payload["binding_id"], f"{path}.binding_id"),
        binding_sha256=_optional_string(
            payload["binding_sha256"],
            f"{path}.binding_sha256",
        ),
        manifest_id=_optional_string(
            payload["manifest_id"],
            f"{path}.manifest_id",
        ),
        manifest_sha256=_optional_string(
            payload["manifest_sha256"],
            f"{path}.manifest_sha256",
        ),
        profile_sha256=_optional_string(
            payload["profile_sha256"],
            f"{path}.profile_sha256",
        ),
        signer_key_id=_optional_string(
            payload["signer_key_id"],
            f"{path}.signer_key_id",
        ),
        signer_owner_id=_optional_string(
            payload["signer_owner_id"],
            f"{path}.signer_owner_id",
        ),
        signer_key_generation=_optional_integer(
            payload["signer_key_generation"],
            f"{path}.signer_key_generation",
        ),
        keyring_generation=_optional_integer(
            payload["keyring_generation"],
            f"{path}.keyring_generation",
        ),
        signature_envelope_sha256=_optional_string(
            payload["signature_envelope_sha256"],
            f"{path}.signature_envelope_sha256",
        ),
        replacement_evidence_id=_optional_string(
            payload["replacement_evidence_id"],
            f"{path}.replacement_evidence_id",
        ),
        reason=_optional_string(payload["reason"], f"{path}.reason"),
        registry_head_sha256=_string(
            payload["registry_head_sha256"],
            f"{path}.registry_head_sha256",
        ),
        state_root_sha256=_string(
            payload["state_root_sha256"],
            f"{path}.state_root_sha256",
        ),
        verified_at=_datetime(payload["verified_at"], f"{path}.verified_at"),
    )


def _decode_evidence_state_proof(
    value: object,
    path: str,
) -> EvidenceStateProof:
    payload = _object(
        value,
        path,
        {
            "evidence_id",
            "record",
            "leaf_index",
            "tree_size",
            "audit_path",
            "state_root_sha256",
        },
    )
    return EvidenceStateProof(
        evidence_id=_string(payload["evidence_id"], f"{path}.evidence_id"),
        record=_decode_evidence_record(payload["record"], f"{path}.record"),
        leaf_index=_integer(payload["leaf_index"], f"{path}.leaf_index"),
        tree_size=_integer(payload["tree_size"], f"{path}.tree_size"),
        audit_path=_tuple_strings(payload["audit_path"], f"{path}.audit_path"),
        state_root_sha256=_string(
            payload["state_root_sha256"],
            f"{path}.state_root_sha256",
        ),
    )


def _decode_evidence_record(
    value: object,
    path: str,
) -> EvidenceRegistryRecord:
    payload = _object(
        value,
        path,
        {
            "evidence_id",
            "binding_id",
            "binding_sha256",
            "manifest_id",
            "manifest_sha256",
            "profile_sha256",
            "subject",
            "subject_version",
            "profile_id",
            "profile_version",
            "signer_key_id",
            "signer_owner_id",
            "signer_key_generation",
            "keyring_generation",
            "signature_envelope_sha256",
            "cryptographically_verified_at",
            "status",
            "registered_at",
            "updated_at",
            "registration_event_sha256",
            "latest_event_sha256",
            "replacement_evidence_id",
            "lifecycle_reason",
        },
    )
    return EvidenceRegistryRecord(
        evidence_id=_string(payload["evidence_id"], f"{path}.evidence_id"),
        binding_id=_string(payload["binding_id"], f"{path}.binding_id"),
        binding_sha256=_string(
            payload["binding_sha256"],
            f"{path}.binding_sha256",
        ),
        manifest_id=_string(payload["manifest_id"], f"{path}.manifest_id"),
        manifest_sha256=_string(
            payload["manifest_sha256"],
            f"{path}.manifest_sha256",
        ),
        profile_sha256=_string(
            payload["profile_sha256"],
            f"{path}.profile_sha256",
        ),
        subject=_string(payload["subject"], f"{path}.subject"),
        subject_version=_string(
            payload["subject_version"],
            f"{path}.subject_version",
        ),
        profile_id=_string(payload["profile_id"], f"{path}.profile_id"),
        profile_version=_string(
            payload["profile_version"],
            f"{path}.profile_version",
        ),
        signer_key_id=_string(
            payload["signer_key_id"],
            f"{path}.signer_key_id",
        ),
        signer_owner_id=_string(
            payload["signer_owner_id"],
            f"{path}.signer_owner_id",
        ),
        signer_key_generation=_integer(
            payload["signer_key_generation"],
            f"{path}.signer_key_generation",
        ),
        keyring_generation=_integer(
            payload["keyring_generation"],
            f"{path}.keyring_generation",
        ),
        signature_envelope_sha256=_string(
            payload["signature_envelope_sha256"],
            f"{path}.signature_envelope_sha256",
        ),
        cryptographically_verified_at=_datetime(
            payload["cryptographically_verified_at"],
            f"{path}.cryptographically_verified_at",
        ),
        status=_enum(
            EvidenceLifecycleStatus,
            payload["status"],
            f"{path}.status",
        ),
        registered_at=_datetime(
            payload["registered_at"],
            f"{path}.registered_at",
        ),
        updated_at=_datetime(payload["updated_at"], f"{path}.updated_at"),
        registration_event_sha256=_string(
            payload["registration_event_sha256"],
            f"{path}.registration_event_sha256",
        ),
        latest_event_sha256=_string(
            payload["latest_event_sha256"],
            f"{path}.latest_event_sha256",
        ),
        replacement_evidence_id=_optional_string(
            payload["replacement_evidence_id"],
            f"{path}.replacement_evidence_id",
        ),
        lifecycle_reason=_optional_string(
            payload["lifecycle_reason"],
            f"{path}.lifecycle_reason",
        ),
    )


def _decode_profile_state_proof(
    value: object,
    *,
    profile: QualificationProfile,
    path: str,
) -> QualificationProfileStateProof:
    payload = _object(
        value,
        path,
        {
            "profile_ref",
            "record",
            "leaf_index",
            "tree_size",
            "audit_path",
            "state_root_sha256",
        },
    )
    return QualificationProfileStateProof(
        profile_ref=_string(payload["profile_ref"], f"{path}.profile_ref"),
        record=_decode_profile_record(
            payload["record"],
            profile=profile,
            path=f"{path}.record",
        ),
        leaf_index=_integer(payload["leaf_index"], f"{path}.leaf_index"),
        tree_size=_integer(payload["tree_size"], f"{path}.tree_size"),
        audit_path=_tuple_strings(payload["audit_path"], f"{path}.audit_path"),
        state_root_sha256=_string(
            payload["state_root_sha256"],
            f"{path}.state_root_sha256",
        ),
    )


def _decode_profile_record(
    value: object,
    *,
    profile: QualificationProfile,
    path: str,
) -> QualificationProfileRecord:
    payload = _object(
        value,
        path,
        {
            "profile_ref",
            "profile_sha256",
            "status",
            "registered_at",
            "updated_at",
            "lifecycle_reason",
            "superseded_by",
        },
    )
    return QualificationProfileRecord(
        profile=profile,
        status=_enum(
            QualificationProfileStatus,
            payload["status"],
            f"{path}.status",
        ),
        registered_at=_datetime(
            payload["registered_at"],
            f"{path}.registered_at",
        ),
        updated_at=_datetime(payload["updated_at"], f"{path}.updated_at"),
        lifecycle_reason=_optional_string(
            payload["lifecycle_reason"],
            f"{path}.lifecycle_reason",
        ),
        superseded_by=_optional_string(
            payload["superseded_by"],
            f"{path}.superseded_by",
        ),
    )


def _decode_transparency_entry(
    value: object,
    path: str,
) -> QualificationTransparencyEntry:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "entry_type",
            "object_id",
            "object_sha256",
            "subject",
            "subject_version",
            "profile_id",
            "profile_version",
            "published_at",
        },
    )
    return QualificationTransparencyEntry(
        entry_type=_string(payload["entry_type"], f"{path}.entry_type"),
        object_id=_string(payload["object_id"], f"{path}.object_id"),
        object_sha256=_string(
            payload["object_sha256"],
            f"{path}.object_sha256",
        ),
        subject=_string(payload["subject"], f"{path}.subject"),
        subject_version=_string(
            payload["subject_version"],
            f"{path}.subject_version",
        ),
        profile_id=_string(payload["profile_id"], f"{path}.profile_id"),
        profile_version=_string(
            payload["profile_version"],
            f"{path}.profile_version",
        ),
        published_at=_datetime(
            payload["published_at"],
            f"{path}.published_at",
        ),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_inclusion_proof(
    value: object,
    path: str,
) -> QualificationInclusionProof:
    payload = _object(
        value,
        path,
        {"tree_size", "leaf_index", "leaf_sha256", "audit_path", "root_sha256"},
    )
    return QualificationInclusionProof(
        tree_size=_integer(payload["tree_size"], f"{path}.tree_size"),
        leaf_index=_integer(payload["leaf_index"], f"{path}.leaf_index"),
        leaf_sha256=_string(payload["leaf_sha256"], f"{path}.leaf_sha256"),
        audit_path=_tuple_strings(payload["audit_path"], f"{path}.audit_path"),
        root_sha256=_string(payload["root_sha256"], f"{path}.root_sha256"),
    )


def _decode_transparency_head(
    value: object,
    path: str,
) -> QualificationTransparencyTreeHead:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "tree_size",
            "root_sha256",
            "previous_tree_head_sha256",
            "issued_at",
        },
    )
    return QualificationTransparencyTreeHead(
        tree_size=_integer(payload["tree_size"], f"{path}.tree_size"),
        root_sha256=_string(payload["root_sha256"], f"{path}.root_sha256"),
        previous_tree_head_sha256=_string(
            payload["previous_tree_head_sha256"],
            f"{path}.previous_tree_head_sha256",
        ),
        issued_at=_datetime(payload["issued_at"], f"{path}.issued_at"),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_consistency_proof(
    value: object,
    path: str,
) -> QualificationDeltaConsistencyProof:
    payload = _object(
        value,
        path,
        {
            "previous_tree_size",
            "current_tree_size",
            "previous_root_sha256",
            "current_root_sha256",
            "previous_leaf_hashes",
            "appended_leaf_hashes",
        },
    )
    return QualificationDeltaConsistencyProof(
        previous_tree_size=_integer(
            payload["previous_tree_size"],
            f"{path}.previous_tree_size",
        ),
        current_tree_size=_integer(
            payload["current_tree_size"],
            f"{path}.current_tree_size",
        ),
        previous_root_sha256=_string(
            payload["previous_root_sha256"],
            f"{path}.previous_root_sha256",
        ),
        current_root_sha256=_string(
            payload["current_root_sha256"],
            f"{path}.current_root_sha256",
        ),
        previous_leaf_hashes=_tuple_strings(
            payload["previous_leaf_hashes"],
            f"{path}.previous_leaf_hashes",
        ),
        appended_leaf_hashes=_tuple_strings(
            payload["appended_leaf_hashes"],
            f"{path}.appended_leaf_hashes",
        ),
    )


def _decode_checkpoint_v3(
    value: object,
    path: str,
) -> QualificationTrustCheckpoint:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "evidence_event_count",
            "evidence_event_head_sha256",
            "evidence_state_root_sha256",
            "profile_record_count",
            "profile_state_root_sha256",
            "transparency_tree_size",
            "transparency_root_sha256",
            "transparency_tree_head_sha256",
            "previous_trust_checkpoint_sha256",
            "issued_at",
        },
    )
    return QualificationTrustCheckpoint(
        evidence_event_count=_integer(
            payload["evidence_event_count"],
            f"{path}.evidence_event_count",
        ),
        evidence_event_head_sha256=_string(
            payload["evidence_event_head_sha256"],
            f"{path}.evidence_event_head_sha256",
        ),
        evidence_state_root_sha256=_string(
            payload["evidence_state_root_sha256"],
            f"{path}.evidence_state_root_sha256",
        ),
        profile_record_count=_integer(
            payload["profile_record_count"],
            f"{path}.profile_record_count",
        ),
        profile_state_root_sha256=_string(
            payload["profile_state_root_sha256"],
            f"{path}.profile_state_root_sha256",
        ),
        transparency_tree_size=_integer(
            payload["transparency_tree_size"],
            f"{path}.transparency_tree_size",
        ),
        transparency_root_sha256=_string(
            payload["transparency_root_sha256"],
            f"{path}.transparency_root_sha256",
        ),
        transparency_tree_head_sha256=_string(
            payload["transparency_tree_head_sha256"],
            f"{path}.transparency_tree_head_sha256",
        ),
        previous_trust_checkpoint_sha256=_string(
            payload["previous_trust_checkpoint_sha256"],
            f"{path}.previous_trust_checkpoint_sha256",
        ),
        issued_at=_datetime(payload["issued_at"], f"{path}.issued_at"),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_signed_checkpoint_v3(
    value: object,
    path: str,
) -> SignedQualificationTrustCheckpoint:
    payload = _object(
        value,
        path,
        {
            "signed_checkpoint_id",
            "checkpoint_id",
            "checkpoint",
            "checkpoint_sha256",
            "signature",
            "signature_envelope_sha256",
        },
    )
    return SignedQualificationTrustCheckpoint(
        checkpoint=_decode_checkpoint_v3(
            payload["checkpoint"],
            f"{path}.checkpoint",
        ),
        envelope=_decode_signature_envelope(
            payload["signature"],
            f"{path}.signature",
        ),
    )


def _decode_keyring_snapshot(
    value: object,
    path: str,
) -> QualificationKeyringSnapshot:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "domain",
            "generation",
            "issued_at",
            "expires_at",
            "root_key_id",
            "keys",
            "root_signature_b64",
            "snapshot_sha256",
        },
    )
    keys = tuple(
        _decode_key_descriptor(item, f"{path}.keys[{index}]")
        for index, item in enumerate(_array(payload["keys"], f"{path}.keys"))
    )
    return QualificationKeyringSnapshot(
        generation=_integer(payload["generation"], f"{path}.generation"),
        issued_at=_datetime(payload["issued_at"], f"{path}.issued_at"),
        expires_at=_datetime(payload["expires_at"], f"{path}.expires_at"),
        root_key_id=_string(payload["root_key_id"], f"{path}.root_key_id"),
        keys=keys,
        root_signature_b64=_string(
            payload["root_signature_b64"],
            f"{path}.root_signature_b64",
        ),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_key_descriptor(
    value: object,
    path: str,
) -> QualificationSigningKeyDescriptor:
    payload = _object(
        value,
        path,
        {
            "key_id",
            "owner_id",
            "backend",
            "generation",
            "public_key_b64",
            "not_before",
            "not_after",
            "revoked_at",
        },
    )
    return QualificationSigningKeyDescriptor(
        key_id=_string(payload["key_id"], f"{path}.key_id"),
        owner_id=_string(payload["owner_id"], f"{path}.owner_id"),
        backend=_string(payload["backend"], f"{path}.backend"),
        generation=_integer(payload["generation"], f"{path}.generation"),
        public_key_b64=_string(
            payload["public_key_b64"],
            f"{path}.public_key_b64",
        ),
        not_before=_datetime(payload["not_before"], f"{path}.not_before"),
        not_after=_datetime(payload["not_after"], f"{path}.not_after"),
        revoked_at=_optional_datetime(
            payload["revoked_at"],
            f"{path}.revoked_at",
        ),
    )


def _decode_profile_event_delta(
    value: object,
    path: str,
) -> QualificationProfileEventDeltaProof:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "previous_event_count",
            "current_event_count",
            "previous_event_head_sha256",
            "current_event_head_sha256",
            "appended_events",
        },
    )
    return QualificationProfileEventDeltaProof(
        previous_event_count=_integer(
            payload["previous_event_count"],
            f"{path}.previous_event_count",
        ),
        current_event_count=_integer(
            payload["current_event_count"],
            f"{path}.current_event_count",
        ),
        previous_event_head_sha256=_string(
            payload["previous_event_head_sha256"],
            f"{path}.previous_event_head_sha256",
        ),
        current_event_head_sha256=_string(
            payload["current_event_head_sha256"],
            f"{path}.current_event_head_sha256",
        ),
        appended_events=tuple(
            _decode_profile_event(item, f"{path}.appended_events[{index}]")
            for index, item in enumerate(
                _array(payload["appended_events"], f"{path}.appended_events")
            )
        ),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_profile_event(
    value: object,
    path: str,
) -> QualificationProfileEvent:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "sequence",
            "event_type",
            "profile_ref",
            "profile_sha256",
            "status",
            "observed_at",
            "previous_event_sha256",
            "reason",
            "superseded_by",
        },
    )
    return QualificationProfileEvent(
        sequence=_integer(payload["sequence"], f"{path}.sequence"),
        event_type=_enum(
            QualificationProfileEventType,
            payload["event_type"],
            f"{path}.event_type",
        ),
        profile_ref=_string(payload["profile_ref"], f"{path}.profile_ref"),
        profile_sha256=_string(
            payload["profile_sha256"],
            f"{path}.profile_sha256",
        ),
        status=_enum(
            QualificationProfileStatus,
            payload["status"],
            f"{path}.status",
        ),
        observed_at=_datetime(payload["observed_at"], f"{path}.observed_at"),
        previous_event_sha256=_string(
            payload["previous_event_sha256"],
            f"{path}.previous_event_sha256",
        ),
        reason=_optional_string(payload["reason"], f"{path}.reason"),
        superseded_by=_optional_string(
            payload["superseded_by"],
            f"{path}.superseded_by",
        ),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_profile_publication_receipt(
    value: object,
    path: str,
) -> QualificationProfilePublicationReceipt:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "profile_event_count",
            "profile_event_head_sha256",
            "published_profile_event_count",
            "transparency_tree_size",
            "transparency_root_sha256",
            "transparency_tree_head_sha256",
            "observed_at",
        },
    )
    return QualificationProfilePublicationReceipt(
        profile_event_count=_integer(
            payload["profile_event_count"],
            f"{path}.profile_event_count",
        ),
        profile_event_head_sha256=_string(
            payload["profile_event_head_sha256"],
            f"{path}.profile_event_head_sha256",
        ),
        published_profile_event_count=_integer(
            payload["published_profile_event_count"],
            f"{path}.published_profile_event_count",
        ),
        transparency_tree_size=_integer(
            payload["transparency_tree_size"],
            f"{path}.transparency_tree_size",
        ),
        transparency_root_sha256=_string(
            payload["transparency_root_sha256"],
            f"{path}.transparency_root_sha256",
        ),
        transparency_tree_head_sha256=_string(
            payload["transparency_tree_head_sha256"],
            f"{path}.transparency_tree_head_sha256",
        ),
        observed_at=_datetime(payload["observed_at"], f"{path}.observed_at"),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_checkpoint_v4(
    value: object,
    path: str,
) -> QualificationTrustCheckpointV4:
    payload = _object(
        value,
        path,
        {
            "schema_version",
            "evidence_event_count",
            "evidence_event_head_sha256",
            "evidence_state_root_sha256",
            "profile_record_count",
            "profile_state_root_sha256",
            "profile_event_count",
            "profile_event_head_sha256",
            "profile_publication_receipt_sha256",
            "profile_publication_tree_size",
            "profile_publication_root_sha256",
            "profile_publication_tree_head_sha256",
            "transparency_tree_size",
            "transparency_root_sha256",
            "transparency_tree_head_sha256",
            "previous_trust_checkpoint_sha256",
            "issued_at",
        },
    )
    return QualificationTrustCheckpointV4(
        evidence_event_count=_integer(
            payload["evidence_event_count"],
            f"{path}.evidence_event_count",
        ),
        evidence_event_head_sha256=_string(
            payload["evidence_event_head_sha256"],
            f"{path}.evidence_event_head_sha256",
        ),
        evidence_state_root_sha256=_string(
            payload["evidence_state_root_sha256"],
            f"{path}.evidence_state_root_sha256",
        ),
        profile_record_count=_integer(
            payload["profile_record_count"],
            f"{path}.profile_record_count",
        ),
        profile_state_root_sha256=_string(
            payload["profile_state_root_sha256"],
            f"{path}.profile_state_root_sha256",
        ),
        profile_event_count=_integer(
            payload["profile_event_count"],
            f"{path}.profile_event_count",
        ),
        profile_event_head_sha256=_string(
            payload["profile_event_head_sha256"],
            f"{path}.profile_event_head_sha256",
        ),
        profile_publication_receipt_sha256=_string(
            payload["profile_publication_receipt_sha256"],
            f"{path}.profile_publication_receipt_sha256",
        ),
        profile_publication_tree_size=_integer(
            payload["profile_publication_tree_size"],
            f"{path}.profile_publication_tree_size",
        ),
        profile_publication_root_sha256=_string(
            payload["profile_publication_root_sha256"],
            f"{path}.profile_publication_root_sha256",
        ),
        profile_publication_tree_head_sha256=_string(
            payload["profile_publication_tree_head_sha256"],
            f"{path}.profile_publication_tree_head_sha256",
        ),
        transparency_tree_size=_integer(
            payload["transparency_tree_size"],
            f"{path}.transparency_tree_size",
        ),
        transparency_root_sha256=_string(
            payload["transparency_root_sha256"],
            f"{path}.transparency_root_sha256",
        ),
        transparency_tree_head_sha256=_string(
            payload["transparency_tree_head_sha256"],
            f"{path}.transparency_tree_head_sha256",
        ),
        previous_trust_checkpoint_sha256=_string(
            payload["previous_trust_checkpoint_sha256"],
            f"{path}.previous_trust_checkpoint_sha256",
        ),
        issued_at=_datetime(payload["issued_at"], f"{path}.issued_at"),
        schema_version=_string(payload["schema_version"], f"{path}.schema_version"),
    )


def _decode_signed_checkpoint_v4(
    value: object,
    path: str,
) -> SignedQualificationTrustCheckpointV4:
    payload = _object(
        value,
        path,
        {
            "signed_checkpoint_id",
            "checkpoint_id",
            "checkpoint",
            "checkpoint_sha256",
            "signature",
            "signature_envelope_sha256",
        },
    )
    return SignedQualificationTrustCheckpointV4(
        checkpoint=_decode_checkpoint_v4(
            payload["checkpoint"],
            f"{path}.checkpoint",
        ),
        envelope=_decode_signature_envelope(
            payload["signature"],
            f"{path}.signature",
        ),
    )


def _object(
    value: object,
    path: str,
    expected_keys: set[str],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationBundleDecodeError(f"{path} must be an object")
    actual = set(value)
    if actual != expected_keys:
        missing = sorted(expected_keys - actual)
        unknown = sorted(actual - expected_keys)
        raise QualificationBundleDecodeError(
            f"{path} fields mismatch; missing={missing}; unknown={unknown}"
        )
    return value


def _array(value: object, path: str) -> tuple[object, ...]:
    if not isinstance(value, tuple | list):
        raise QualificationBundleDecodeError(f"{path} must be an array")
    return tuple(value)


def _tuple_strings(value: object, path: str) -> tuple[str, ...]:
    return tuple(
        _string(item, f"{path}[{index}]")
        for index, item in enumerate(_array(value, path))
    )


def _string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise QualificationBundleDecodeError(f"{path} must be a string")
    return value


def _optional_string(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path)


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise QualificationBundleDecodeError(f"{path} must be an integer")
    return value


def _optional_integer(value: object, path: str) -> int | None:
    if value is None:
        return None
    return _integer(value, path)


def _datetime(value: object, path: str) -> datetime:
    text = _string(value, path)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise QualificationBundleDecodeError(
            f"{path} must be an ISO-8601 datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QualificationBundleDecodeError(f"{path} must be timezone-aware")
    return parsed


def _optional_datetime(value: object, path: str) -> datetime | None:
    if value is None:
        return None
    return _datetime(value, path)


def _enum(enum_type: type[_E], value: object, path: str) -> _E:
    text = _string(value, path)
    try:
        return enum_type(text)
    except ValueError as exc:
        raise QualificationBundleDecodeError(
            f"{path} has unsupported value {text!r}"
        ) from exc
