from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from app.qualification.evidence_registry import EvidenceVerificationStatus
from app.qualification.portable_verification_v3 import (
    PortableQualificationVerificationBundleV3,
    PortableQualificationVerificationErrorV3,
    PortableVerificationFailureCodeV3,
    verify_portable_qualification_bundle_v3,
)
from app.qualification.profile_registry import QualificationProfileStatus


class QualificationVerificationOutcomeV3(StrEnum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class QualificationVerificationRequestV3:
    bundle: PortableQualificationVerificationBundleV3
    previous_keyring_generation: int
    observed_at: datetime

    def validate(self) -> None:
        if self.previous_keyring_generation < 0:
            raise ValueError("previous_keyring_generation must be non-negative")
        _aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class QualificationVerificationServiceResultV3:
    outcome: QualificationVerificationOutcomeV3
    usable: bool
    evidence_lifecycle_status: EvidenceVerificationStatus | None
    profile_lifecycle_status: QualificationProfileStatus | None
    failure_code: PortableVerificationFailureCodeV3 | None
    failure_detail: str | None
    bundle_id: str | None
    bundle_sha256: str | None
    evidence_id: str | None
    binding_id: str | None
    manifest_id: str | None
    profile_ref: str | None
    replacement_evidence_id: str | None
    evidence_signer_key_id: str | None
    checkpoint_signer_key_id: str | None
    keyring_generation: int | None
    evidence_registry_state_root_sha256: str | None
    profile_registry_state_root_sha256: str | None
    transparency_root_sha256: str | None
    transparency_tree_size: int | None
    verified_at: datetime

    def validate(self) -> None:
        _aware(self.verified_at, "verified_at")
        if self.outcome is QualificationVerificationOutcomeV3.VERIFIED:
            if self.failure_code is not None or self.failure_detail is not None:
                raise ValueError("VERIFIED v3 result cannot carry failure")
            if self.evidence_lifecycle_status is None:
                raise ValueError("VERIFIED v3 result requires evidence lifecycle")
            if self.profile_lifecycle_status is None:
                raise ValueError("VERIFIED v3 result requires profile lifecycle")
            required = (
                self.bundle_id,
                self.bundle_sha256,
                self.evidence_id,
                self.binding_id,
                self.manifest_id,
                self.profile_ref,
                self.evidence_signer_key_id,
                self.checkpoint_signer_key_id,
                self.evidence_registry_state_root_sha256,
                self.profile_registry_state_root_sha256,
                self.transparency_root_sha256,
            )
            if any(value is None or not value for value in required):
                raise ValueError("VERIFIED v3 result is missing verified identity")
            if self.keyring_generation is None or self.keyring_generation < 1:
                raise ValueError("VERIFIED v3 result requires keyring generation")
            if self.transparency_tree_size is None or self.transparency_tree_size < 0:
                raise ValueError("VERIFIED v3 result requires transparency tree size")
            expected_usable = (
                self.evidence_lifecycle_status is EvidenceVerificationStatus.VALID
                and self.profile_lifecycle_status is QualificationProfileStatus.ACTIVE
            )
            if self.usable is not expected_usable:
                raise ValueError("v3 usable flag disagrees with lifecycle state")
        else:
            if self.failure_code is None or not (self.failure_detail or "").strip():
                raise ValueError("REJECTED v3 result requires failure code and detail")
            if self.usable:
                raise ValueError("REJECTED v3 result cannot be usable")
            if self.evidence_lifecycle_status is not None:
                raise ValueError("REJECTED v3 result cannot claim evidence lifecycle")
            if self.profile_lifecycle_status is not None:
                raise ValueError("REJECTED v3 result cannot claim profile lifecycle")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "outcome": self.outcome.value,
            "usable": self.usable,
            "evidence_lifecycle_status": (
                None
                if self.evidence_lifecycle_status is None
                else self.evidence_lifecycle_status.value
            ),
            "profile_lifecycle_status": (
                None
                if self.profile_lifecycle_status is None
                else self.profile_lifecycle_status.value
            ),
            "failure_code": (
                None if self.failure_code is None else self.failure_code.value
            ),
            "failure_detail": self.failure_detail,
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "evidence_id": self.evidence_id,
            "binding_id": self.binding_id,
            "manifest_id": self.manifest_id,
            "profile_ref": self.profile_ref,
            "replacement_evidence_id": self.replacement_evidence_id,
            "evidence_signer_key_id": self.evidence_signer_key_id,
            "checkpoint_signer_key_id": self.checkpoint_signer_key_id,
            "keyring_generation": self.keyring_generation,
            "evidence_registry_state_root_sha256": (
                self.evidence_registry_state_root_sha256
            ),
            "profile_registry_state_root_sha256": (
                self.profile_registry_state_root_sha256
            ),
            "transparency_root_sha256": self.transparency_root_sha256,
            "transparency_tree_size": self.transparency_tree_size,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


class QualificationVerificationServiceV3:
    """Transport-neutral verifier for profile-aware qualification bundles."""

    def __init__(
        self,
        *,
        trusted_root_public_keys: Mapping[str, bytes],
        max_clock_skew_seconds: int = 5,
    ) -> None:
        if not trusted_root_public_keys:
            raise ValueError("qualification verification v3 requires trusted roots")
        if max_clock_skew_seconds < 0:
            raise ValueError("max_clock_skew_seconds must be non-negative")
        roots: dict[str, bytes] = {}
        for key_id, public_key in trusted_root_public_keys.items():
            if not key_id.strip():
                raise ValueError("trusted qualification root key id is required")
            if not isinstance(public_key, bytes) or len(public_key) != 32:
                raise ValueError("trusted qualification root must be 32-byte Ed25519 key")
            roots[key_id] = bytes(public_key)
        self._trusted_roots = MappingProxyType(roots)
        self._max_clock_skew_seconds = max_clock_skew_seconds

    def verify(
        self,
        request: QualificationVerificationRequestV3,
    ) -> QualificationVerificationServiceResultV3:
        request.validate()
        verified_at = _aware(request.observed_at, "observed_at")
        try:
            result = verify_portable_qualification_bundle_v3(
                bundle=request.bundle,
                trusted_root_public_keys=self._trusted_roots,
                previous_keyring_generation=request.previous_keyring_generation,
                observed_at=verified_at,
                max_clock_skew_seconds=self._max_clock_skew_seconds,
            )
        except PortableQualificationVerificationErrorV3 as exc:
            rejected = QualificationVerificationServiceResultV3(
                outcome=QualificationVerificationOutcomeV3.REJECTED,
                usable=False,
                evidence_lifecycle_status=None,
                profile_lifecycle_status=None,
                failure_code=exc.code,
                failure_detail=exc.detail,
                bundle_id=None,
                bundle_sha256=None,
                evidence_id=None,
                binding_id=None,
                manifest_id=None,
                profile_ref=None,
                replacement_evidence_id=None,
                evidence_signer_key_id=None,
                checkpoint_signer_key_id=None,
                keyring_generation=None,
                evidence_registry_state_root_sha256=None,
                profile_registry_state_root_sha256=None,
                transparency_root_sha256=None,
                transparency_tree_size=None,
                verified_at=verified_at,
            )
            rejected.validate()
            return rejected

        accepted = QualificationVerificationServiceResultV3(
            outcome=QualificationVerificationOutcomeV3.VERIFIED,
            usable=result.usable,
            evidence_lifecycle_status=result.evidence_lifecycle_status,
            profile_lifecycle_status=result.profile_lifecycle_status,
            failure_code=None,
            failure_detail=None,
            bundle_id=result.bundle_id,
            bundle_sha256=result.bundle_sha256,
            evidence_id=result.evidence_id,
            binding_id=result.binding_id,
            manifest_id=result.manifest_id,
            profile_ref=result.profile_ref,
            replacement_evidence_id=result.replacement_evidence_id,
            evidence_signer_key_id=result.evidence_signer_key_id,
            checkpoint_signer_key_id=result.checkpoint_signer_key_id,
            keyring_generation=result.keyring_generation,
            evidence_registry_state_root_sha256=(
                result.evidence_registry_state_root_sha256
            ),
            profile_registry_state_root_sha256=(
                result.profile_registry_state_root_sha256
            ),
            transparency_root_sha256=result.transparency_root_sha256,
            transparency_tree_size=result.transparency_tree_size,
            verified_at=result.verified_at,
        )
        accepted.validate()
        return accepted


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
