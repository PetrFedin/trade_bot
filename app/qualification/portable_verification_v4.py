from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.qualification.portable_verification_v3 import (
    PortableQualificationVerificationBundleV3,
    PortableQualificationVerificationErrorV3,
    PortableQualificationVerificationResultV3,
    verify_portable_qualification_bundle_v3,
)
from app.qualification.profile_event_delta import (
    QualificationProfileEventDeltaProof,
    VerifiedQualificationProfileEventDelta,
    verify_profile_event_delta_from_trusted_anchor,
)
from app.qualification.profile_transparency import (
    QualificationProfilePublicationReceipt,
    profile_event_object_id,
)
from app.qualification.signing_authority import verify_qualification_keyring
from app.qualification.transparency_log import (
    QualificationDeltaConsistencyProof,
    QualificationInclusionProof,
    QualificationTransparencyEntry,
    QualificationTransparencyTreeHead,
    transparency_root_at_size_from_consistency,
    verify_delta_consistency,
    verify_inclusion_proof,
)
from app.qualification.trust_checkpoint_v4 import (
    SignedQualificationTrustCheckpointV4,
    verify_trust_checkpoint_v4,
)

_SCHEMA_VERSION = "astra-portable-qualification-verification-v4"


class PortableVerificationFailureCodeV4(StrEnum):
    BASE_V3_REJECTED = "BASE_V3_REJECTED"
    BUNDLE_INVALID = "BUNDLE_INVALID"
    PROFILE_DELTA_REJECTED = "PROFILE_DELTA_REJECTED"
    PROFILE_PUBLICATION_REJECTED = "PROFILE_PUBLICATION_REJECTED"
    TRANSPARENCY_CONTINUITY_REJECTED = "TRANSPARENCY_CONTINUITY_REJECTED"
    TRUST_CHECKPOINT_V4_REJECTED = "TRUST_CHECKPOINT_V4_REJECTED"
    V4_BINDING_MISMATCH = "V4_BINDING_MISMATCH"


class PortableQualificationVerificationErrorV4(ValueError):
    def __init__(
        self,
        code: PortableVerificationFailureCodeV4,
        detail: str,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PortableQualificationVerificationBundleV4:
    base_v3: PortableQualificationVerificationBundleV3
    profile_event_delta: QualificationProfileEventDeltaProof
    profile_event_entries: tuple[QualificationTransparencyEntry, ...]
    profile_event_inclusion_proofs: tuple[QualificationInclusionProof, ...]
    profile_publication_receipt: QualificationProfilePublicationReceipt
    profile_publication_head: QualificationTransparencyTreeHead
    transparency_consistency_proof: QualificationDeltaConsistencyProof
    current_transparency_head: QualificationTransparencyTreeHead
    signed_trust_checkpoint_v4: SignedQualificationTrustCheckpointV4
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("portable qualification v4 bundle schema mismatch")
        self.base_v3.validate()
        self.profile_event_delta.validate()
        self.profile_publication_receipt.validate()
        self.profile_publication_head.validate()
        self.transparency_consistency_proof.validate()
        self.current_transparency_head.validate()
        self.signed_trust_checkpoint_v4.validate()

        if len(self.profile_event_entries) != len(
            self.profile_event_delta.appended_events
        ):
            raise ValueError("portable v4 profile event entry count mismatch")
        if len(self.profile_event_inclusion_proofs) != len(
            self.profile_event_delta.appended_events
        ):
            raise ValueError("portable v4 profile event proof count mismatch")

        receipt = self.profile_publication_receipt
        delta = self.profile_event_delta
        checkpoint = self.signed_trust_checkpoint_v4.checkpoint

        if receipt.profile_event_count != delta.current_event_count:
            raise ValueError("portable v4 receipt profile event count mismatch")
        if receipt.profile_event_head_sha256 != delta.current_event_head_sha256:
            raise ValueError("portable v4 receipt profile event head mismatch")

        if checkpoint.profile_event_count != delta.current_event_count:
            raise ValueError("portable v4 checkpoint profile event count mismatch")
        if checkpoint.profile_event_head_sha256 != delta.current_event_head_sha256:
            raise ValueError("portable v4 checkpoint profile event head mismatch")
        if (
            checkpoint.profile_publication_receipt_sha256
            != receipt.receipt_sha256
        ):
            raise ValueError("portable v4 checkpoint publication receipt mismatch")

        if (
            checkpoint.profile_state_root_sha256
            != self.base_v3.profile_state_proof.state_root_sha256
        ):
            raise ValueError("portable v4 checkpoint profile state root mismatch")
        if (
            checkpoint.evidence_state_root_sha256
            != self.base_v3.evidence_state_proof.state_root_sha256
        ):
            raise ValueError("portable v4 checkpoint evidence state root mismatch")
        if (
            checkpoint.evidence_event_head_sha256
            != self.base_v3.registry_decision.registry_head_sha256
        ):
            raise ValueError("portable v4 checkpoint evidence event head mismatch")

        if (
            self.profile_publication_head.tree_size
            != receipt.transparency_tree_size
        ):
            raise ValueError("portable v4 publication head size mismatch")
        if self.profile_publication_head.root_sha256 != receipt.transparency_root_sha256:
            raise ValueError("portable v4 publication head root mismatch")
        if (
            self.profile_publication_head.tree_head_sha256
            != receipt.transparency_tree_head_sha256
        ):
            raise ValueError("portable v4 publication tree head mismatch")

        if (
            checkpoint.profile_publication_tree_size
            != self.profile_publication_head.tree_size
        ):
            raise ValueError("portable v4 signed publication tree size mismatch")
        if (
            checkpoint.profile_publication_root_sha256
            != self.profile_publication_head.root_sha256
        ):
            raise ValueError("portable v4 signed publication root mismatch")
        if (
            checkpoint.profile_publication_tree_head_sha256
            != self.profile_publication_head.tree_head_sha256
        ):
            raise ValueError("portable v4 signed publication tree head mismatch")

        if checkpoint.transparency_tree_size != self.current_transparency_head.tree_size:
            raise ValueError("portable v4 current transparency size mismatch")
        if checkpoint.transparency_root_sha256 != self.current_transparency_head.root_sha256:
            raise ValueError("portable v4 current transparency root mismatch")
        if (
            checkpoint.transparency_tree_head_sha256
            != self.current_transparency_head.tree_head_sha256
        ):
            raise ValueError("portable v4 current transparency head mismatch")

        consistency = self.transparency_consistency_proof
        if consistency.previous_tree_size != self.base_v3.transparency_head.tree_size:
            raise ValueError("portable v4 consistency previous size mismatch")
        if consistency.previous_root_sha256 != self.base_v3.transparency_head.root_sha256:
            raise ValueError("portable v4 consistency previous root mismatch")
        if consistency.current_tree_size != self.current_transparency_head.tree_size:
            raise ValueError("portable v4 consistency current size mismatch")
        if consistency.current_root_sha256 != self.current_transparency_head.root_sha256:
            raise ValueError("portable v4 consistency current root mismatch")

        derived_publication_root = transparency_root_at_size_from_consistency(
            consistency,
            tree_size=self.profile_publication_head.tree_size,
        )
        if derived_publication_root != self.profile_publication_head.root_sha256:
            raise ValueError("portable v4 publication root is not on transparency path")

        for event, entry, proof in zip(
            delta.appended_events,
            self.profile_event_entries,
            self.profile_event_inclusion_proofs,
            strict=True,
        ):
            entry.validate()
            proof.validate()
            if entry.entry_type != "QUALIFICATION_PROFILE_EVENT":
                raise ValueError("portable v4 profile event entry type mismatch")
            if entry.object_id != profile_event_object_id(event):
                raise ValueError("portable v4 profile event object id mismatch")
            if entry.object_sha256 != event.event_sha256:
                raise ValueError("portable v4 profile event digest mismatch")
            profile_id, profile_version = _split_profile_ref(event.profile_ref)
            if entry.subject != "QUALIFICATION_PROFILE":
                raise ValueError("portable v4 profile event subject mismatch")
            if entry.subject_version != event.event_type.value:
                raise ValueError("portable v4 profile event type mismatch")
            if entry.profile_id != profile_id or entry.profile_version != profile_version:
                raise ValueError("portable v4 profile event profile identity mismatch")
            if _aware(entry.published_at, "entry.published_at") < _aware(
                event.observed_at,
                "event.observed_at",
            ):
                raise ValueError("portable v4 profile event publication predates event")
            if proof.leaf_sha256 != entry.leaf_sha256:
                raise ValueError("portable v4 profile event leaf mismatch")
            if proof.tree_size != self.current_transparency_head.tree_size:
                raise ValueError("portable v4 profile event proof size mismatch")
            if proof.root_sha256 != self.current_transparency_head.root_sha256:
                raise ValueError("portable v4 profile event proof root mismatch")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "base_v3": self.base_v3.payload(),
            "profile_event_delta": self.profile_event_delta.payload(),
            "profile_event_entries": [
                entry.payload() for entry in self.profile_event_entries
            ],
            "profile_event_inclusion_proofs": [
                {
                    "tree_size": proof.tree_size,
                    "leaf_index": proof.leaf_index,
                    "leaf_sha256": proof.leaf_sha256,
                    "audit_path": list(proof.audit_path),
                    "root_sha256": proof.root_sha256,
                }
                for proof in self.profile_event_inclusion_proofs
            ],
            "profile_publication_receipt": self.profile_publication_receipt.payload(),
            "profile_publication_head": self.profile_publication_head.payload(),
            "transparency_consistency_proof": {
                "previous_tree_size": (
                    self.transparency_consistency_proof.previous_tree_size
                ),
                "current_tree_size": (
                    self.transparency_consistency_proof.current_tree_size
                ),
                "previous_root_sha256": (
                    self.transparency_consistency_proof.previous_root_sha256
                ),
                "current_root_sha256": (
                    self.transparency_consistency_proof.current_root_sha256
                ),
                "previous_leaf_hashes": list(
                    self.transparency_consistency_proof.previous_leaf_hashes
                ),
                "appended_leaf_hashes": list(
                    self.transparency_consistency_proof.appended_leaf_hashes
                ),
            },
            "current_transparency_head": self.current_transparency_head.payload(),
            "signed_trust_checkpoint_v4": self.signed_trust_checkpoint_v4.payload(),
        }

    @property
    def bundle_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def bundle_id(self) -> str:
        return f"qverifyv4_{self.bundle_sha256[:24]}"


@dataclass(frozen=True)
class PortableQualificationVerificationResultV4:
    base_v3_result: PortableQualificationVerificationResultV3
    bundle_id: str
    bundle_sha256: str
    trusted_previous_profile_event_count: int
    trusted_previous_profile_event_head_sha256: str
    current_profile_event_count: int
    current_profile_event_head_sha256: str
    appended_profile_event_count: int
    profile_publication_receipt_sha256: str
    checkpoint_v4_id: str
    checkpoint_v4_signer_key_id: str
    current_transparency_tree_size: int
    current_transparency_root_sha256: str
    usable: bool
    verified_at: datetime

    def payload(self) -> dict[str, object]:
        return {
            "base_v3_result": self.base_v3_result.payload(),
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "trusted_previous_profile_event_count": (
                self.trusted_previous_profile_event_count
            ),
            "trusted_previous_profile_event_head_sha256": (
                self.trusted_previous_profile_event_head_sha256
            ),
            "current_profile_event_count": self.current_profile_event_count,
            "current_profile_event_head_sha256": self.current_profile_event_head_sha256,
            "appended_profile_event_count": self.appended_profile_event_count,
            "profile_publication_receipt_sha256": (
                self.profile_publication_receipt_sha256
            ),
            "checkpoint_v4_id": self.checkpoint_v4_id,
            "checkpoint_v4_signer_key_id": self.checkpoint_v4_signer_key_id,
            "current_transparency_tree_size": self.current_transparency_tree_size,
            "current_transparency_root_sha256": self.current_transparency_root_sha256,
            "usable": self.usable,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


def verify_portable_qualification_bundle_v4(
    *,
    bundle: PortableQualificationVerificationBundleV4,
    trusted_root_public_keys: Mapping[str, bytes],
    previous_keyring_generation: int,
    trusted_previous_profile_event_count: int,
    trusted_previous_profile_event_head_sha256: str,
    observed_at: datetime,
    max_clock_skew_seconds: int = 5,
) -> PortableQualificationVerificationResultV4:
    try:
        bundle.validate()
        now = _aware(observed_at, "observed_at")
    except ValueError as exc:
        raise PortableQualificationVerificationErrorV4(
            PortableVerificationFailureCodeV4.BUNDLE_INVALID,
            str(exc),
        ) from exc

    try:
        base_result = verify_portable_qualification_bundle_v3(
            bundle=bundle.base_v3,
            trusted_root_public_keys=trusted_root_public_keys,
            previous_keyring_generation=previous_keyring_generation,
            observed_at=now,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
    except PortableQualificationVerificationErrorV3 as exc:
        raise PortableQualificationVerificationErrorV4(
            PortableVerificationFailureCodeV4.BASE_V3_REJECTED,
            f"{exc.code.value}: {exc.detail}",
        ) from exc

    try:
        delta: VerifiedQualificationProfileEventDelta = (
            verify_profile_event_delta_from_trusted_anchor(
                bundle.profile_event_delta,
                trusted_previous_event_count=trusted_previous_profile_event_count,
                trusted_previous_event_head_sha256=(
                    trusted_previous_profile_event_head_sha256
                ),
            )
        )
    except ValueError as exc:
        raise PortableQualificationVerificationErrorV4(
            PortableVerificationFailureCodeV4.PROFILE_DELTA_REJECTED,
            str(exc),
        ) from exc

    if not verify_delta_consistency(bundle.transparency_consistency_proof):
        raise PortableQualificationVerificationErrorV4(
            PortableVerificationFailureCodeV4.TRANSPARENCY_CONTINUITY_REJECTED,
            "portable v4 transparency consistency proof is invalid",
        )

    for proof in bundle.profile_event_inclusion_proofs:
        if not verify_inclusion_proof(proof):
            raise PortableQualificationVerificationErrorV4(
                PortableVerificationFailureCodeV4.PROFILE_PUBLICATION_REJECTED,
                "portable v4 profile event inclusion proof is invalid",
            )

    try:
        keyring = verify_qualification_keyring(
            bundle.base_v3.keyring_snapshot,
            trusted_root_public_keys=trusted_root_public_keys,
            previous_generation=previous_keyring_generation,
            observed_at=now,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
        checkpoint = verify_trust_checkpoint_v4(
            signed_checkpoint=bundle.signed_trust_checkpoint_v4,
            keyring=keyring,
            observed_at=now,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
    except ValueError as exc:
        raise PortableQualificationVerificationErrorV4(
            PortableVerificationFailureCodeV4.TRUST_CHECKPOINT_V4_REJECTED,
            str(exc),
        ) from exc

    binding_mismatch = (
        checkpoint.profile_event_count != delta.current_event_count
        or checkpoint.profile_event_head_sha256 != delta.current_event_head_sha256
        or checkpoint.profile_publication_receipt_sha256
        != bundle.profile_publication_receipt.receipt_sha256
        or checkpoint.transparency_tree_size
        != bundle.current_transparency_head.tree_size
        or checkpoint.transparency_root_sha256
        != bundle.current_transparency_head.root_sha256
        or checkpoint.transparency_tree_head_sha256
        != bundle.current_transparency_head.tree_head_sha256
    )
    if binding_mismatch:
        raise PortableQualificationVerificationErrorV4(
            PortableVerificationFailureCodeV4.V4_BINDING_MISMATCH,
            "portable v4 profile lifecycle or publication binding mismatch",
        )

    usable = base_result.usable
    return PortableQualificationVerificationResultV4(
        base_v3_result=base_result,
        bundle_id=bundle.bundle_id,
        bundle_sha256=bundle.bundle_sha256,
        trusted_previous_profile_event_count=trusted_previous_profile_event_count,
        trusted_previous_profile_event_head_sha256=(
            trusted_previous_profile_event_head_sha256
        ),
        current_profile_event_count=delta.current_event_count,
        current_profile_event_head_sha256=delta.current_event_head_sha256,
        appended_profile_event_count=delta.appended_event_count,
        profile_publication_receipt_sha256=(
            bundle.profile_publication_receipt.receipt_sha256
        ),
        checkpoint_v4_id=checkpoint.checkpoint_id,
        checkpoint_v4_signer_key_id=checkpoint.signer_key_id,
        current_transparency_tree_size=checkpoint.transparency_tree_size,
        current_transparency_root_sha256=checkpoint.transparency_root_sha256,
        usable=usable,
        verified_at=now,
    )


def _split_profile_ref(profile_ref: str) -> tuple[str, str]:
    profile_id, separator, version = profile_ref.rpartition("@")
    if not separator or not profile_id.strip() or not version.strip():
        raise ValueError("qualification profile_ref must be profile_id@version")
    return profile_id, version


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
