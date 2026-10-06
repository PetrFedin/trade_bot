from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from app.qualification.evidence_registry import EvidenceVerificationStatus
from app.qualification.portable_verification import (
    PortableQualificationVerificationBundle,
    PortableQualificationVerificationError,
    PortableVerificationFailureCode,
    verify_portable_qualification_bundle,
)


class QualificationVerificationOutcome(StrEnum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class QualificationVerificationRequest:
    bundle: PortableQualificationVerificationBundle
    previous_keyring_generation: int
    observed_at: datetime

    def validate(self) -> None:
        if self.previous_keyring_generation < 0:
            raise ValueError("previous_keyring_generation must be non-negative")
        _aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class QualificationVerificationServiceResult:
    outcome: QualificationVerificationOutcome
    usable: bool
    lifecycle_status: EvidenceVerificationStatus | None
    failure_code: PortableVerificationFailureCode | None
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
    registry_event_head_sha256: str | None
    registry_state_root_sha256: str | None
    transparency_root_sha256: str | None
    transparency_tree_size: int | None
    verified_at: datetime

    def validate(self) -> None:
        _aware(self.verified_at, "verified_at")
        if self.outcome is QualificationVerificationOutcome.VERIFIED:
            if self.failure_code is not None or self.failure_detail is not None:
                raise ValueError("VERIFIED result cannot carry failure")
            if self.lifecycle_status is None:
                raise ValueError("VERIFIED result requires lifecycle status")
            required = (
                self.bundle_id,
                self.bundle_sha256,
                self.evidence_id,
                self.binding_id,
                self.manifest_id,
                self.profile_ref,
                self.evidence_signer_key_id,
                self.checkpoint_signer_key_id,
                self.registry_event_head_sha256,
                self.registry_state_root_sha256,
                self.transparency_root_sha256,
            )
            if any(value is None or not value for value in required):
                raise ValueError("VERIFIED result is missing verified identity")
            if self.keyring_generation is None or self.keyring_generation < 1:
                raise ValueError("VERIFIED result requires keyring generation")
            if self.transparency_tree_size is None or self.transparency_tree_size < 0:
                raise ValueError("VERIFIED result requires transparency tree size")
            expected_usable = self.lifecycle_status is EvidenceVerificationStatus.VALID
            if self.usable is not expected_usable:
                raise ValueError("usable flag disagrees with lifecycle status")
        else:
            if self.failure_code is None or not (self.failure_detail or "").strip():
                raise ValueError("REJECTED result requires failure code and detail")
            if self.usable:
                raise ValueError("REJECTED result cannot be usable")
            if self.lifecycle_status is not None:
                raise ValueError("REJECTED result cannot claim lifecycle status")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "outcome": self.outcome.value,
            "usable": self.usable,
            "lifecycle_status": (
                None if self.lifecycle_status is None else self.lifecycle_status.value
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
            "registry_event_head_sha256": self.registry_event_head_sha256,
            "registry_state_root_sha256": self.registry_state_root_sha256,
            "transparency_root_sha256": self.transparency_root_sha256,
            "transparency_tree_size": self.transparency_tree_size,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


class QualificationVerificationService:
    """Transport-neutral verification authority for portable qualification bundles."""

    def __init__(
        self,
        *,
        trusted_root_public_keys: Mapping[str, bytes],
        max_clock_skew_seconds: int = 5,
    ) -> None:
        if not trusted_root_public_keys:
            raise ValueError("qualification verification requires trusted roots")
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
        request: QualificationVerificationRequest,
    ) -> QualificationVerificationServiceResult:
        request.validate()
        verified_at = _aware(request.observed_at, "observed_at")
        try:
            result = verify_portable_qualification_bundle(
                bundle=request.bundle,
                trusted_root_public_keys=self._trusted_roots,
                previous_keyring_generation=request.previous_keyring_generation,
                observed_at=verified_at,
                max_clock_skew_seconds=self._max_clock_skew_seconds,
            )
        except PortableQualificationVerificationError as exc:
            rejected = QualificationVerificationServiceResult(
                outcome=QualificationVerificationOutcome.REJECTED,
                usable=False,
                lifecycle_status=None,
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
                registry_event_head_sha256=None,
                registry_state_root_sha256=None,
                transparency_root_sha256=None,
                transparency_tree_size=None,
                verified_at=verified_at,
            )
            rejected.validate()
            return rejected

        accepted = QualificationVerificationServiceResult(
            outcome=QualificationVerificationOutcome.VERIFIED,
            usable=result.lifecycle_status is EvidenceVerificationStatus.VALID,
            lifecycle_status=result.lifecycle_status,
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
            registry_event_head_sha256=result.registry_event_head_sha256,
            registry_state_root_sha256=result.registry_state_root_sha256,
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
