from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from app.qualification.portable_verification_v4 import (
    PortableQualificationVerificationBundleV4,
    PortableQualificationVerificationErrorV4,
    PortableVerificationFailureCodeV4,
    verify_portable_qualification_bundle_v4,
)


class QualificationVerificationOutcomeV4(StrEnum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class QualificationTrustStateV4:
    profile_event_count: int
    profile_event_head_sha256: str
    transparency_tree_size: int
    transparency_root_sha256: str
    checkpoint_v4_sha256: str

    def validate(self) -> None:
        if self.profile_event_count < 0:
            raise ValueError("profile_event_count must be non-negative")
        if self.transparency_tree_size < 0:
            raise ValueError("transparency_tree_size must be non-negative")
        for name, value in (
            ("profile_event_head_sha256", self.profile_event_head_sha256),
            ("transparency_root_sha256", self.transparency_root_sha256),
            ("checkpoint_v4_sha256", self.checkpoint_v4_sha256),
        ):
            _digest(value, name)
        if self.profile_event_count == 0 and self.profile_event_head_sha256 != "0" * 64:
            raise ValueError("zero profile event count requires genesis head")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "profile_event_count": self.profile_event_count,
            "profile_event_head_sha256": self.profile_event_head_sha256,
            "transparency_tree_size": self.transparency_tree_size,
            "transparency_root_sha256": self.transparency_root_sha256,
            "checkpoint_v4_sha256": self.checkpoint_v4_sha256,
        }


@dataclass(frozen=True)
class QualificationVerificationRequestV4:
    bundle: PortableQualificationVerificationBundleV4
    previous_keyring_generation: int
    trusted_state: QualificationTrustStateV4
    observed_at: datetime

    def validate(self) -> None:
        if self.previous_keyring_generation < 0:
            raise ValueError("previous_keyring_generation must be non-negative")
        self.trusted_state.validate()
        _aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class QualificationVerificationServiceResultV4:
    outcome: QualificationVerificationOutcomeV4
    usable: bool
    failure_code: PortableVerificationFailureCodeV4 | None
    failure_detail: str | None
    bundle_id: str | None
    bundle_sha256: str | None
    checkpoint_v4_id: str | None
    checkpoint_v4_signer_key_id: str | None
    previous_trust_state: QualificationTrustStateV4
    next_trust_state: QualificationTrustStateV4 | None
    verified_at: datetime

    def validate(self) -> None:
        self.previous_trust_state.validate()
        _aware(self.verified_at, "verified_at")
        if self.outcome is QualificationVerificationOutcomeV4.VERIFIED:
            if self.failure_code is not None or self.failure_detail is not None:
                raise ValueError("VERIFIED v4 result cannot carry failure")
            if not self.bundle_id or not self.bundle_sha256:
                raise ValueError("VERIFIED v4 result requires bundle identity")
            if not self.checkpoint_v4_id or not self.checkpoint_v4_signer_key_id:
                raise ValueError("VERIFIED v4 result requires checkpoint identity")
            if self.next_trust_state is None:
                raise ValueError("VERIFIED v4 result requires next trust state")
            self.next_trust_state.validate()
        else:
            if self.failure_code is None or not (self.failure_detail or "").strip():
                raise ValueError("REJECTED v4 result requires failure code and detail")
            if self.usable:
                raise ValueError("REJECTED v4 result cannot be usable")
            if self.next_trust_state is not None:
                raise ValueError("REJECTED v4 result cannot advance trust state")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "outcome": self.outcome.value,
            "usable": self.usable,
            "failure_code": (
                None if self.failure_code is None else self.failure_code.value
            ),
            "failure_detail": self.failure_detail,
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "checkpoint_v4_id": self.checkpoint_v4_id,
            "checkpoint_v4_signer_key_id": self.checkpoint_v4_signer_key_id,
            "previous_trust_state": self.previous_trust_state.payload(),
            "next_trust_state": (
                None
                if self.next_trust_state is None
                else self.next_trust_state.payload()
            ),
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


class QualificationVerificationServiceV4:
    """Transport-neutral stateful verifier for incremental qualification updates."""

    def __init__(
        self,
        *,
        trusted_root_public_keys: Mapping[str, bytes],
        max_clock_skew_seconds: int = 5,
    ) -> None:
        if not trusted_root_public_keys:
            raise ValueError("qualification verification v4 requires trusted roots")
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
        request: QualificationVerificationRequestV4,
    ) -> QualificationVerificationServiceResultV4:
        request.validate()
        verified_at = _aware(request.observed_at, "observed_at")
        previous = request.trusted_state

        try:
            result = verify_portable_qualification_bundle_v4(
                bundle=request.bundle,
                trusted_root_public_keys=self._trusted_roots,
                previous_keyring_generation=request.previous_keyring_generation,
                trusted_previous_profile_event_count=previous.profile_event_count,
                trusted_previous_profile_event_head_sha256=(
                    previous.profile_event_head_sha256
                ),
                trusted_previous_transparency_tree_size=(
                    previous.transparency_tree_size
                ),
                trusted_previous_transparency_root_sha256=(
                    previous.transparency_root_sha256
                ),
                trusted_previous_checkpoint_v4_sha256=(
                    previous.checkpoint_v4_sha256
                ),
                observed_at=verified_at,
                max_clock_skew_seconds=self._max_clock_skew_seconds,
            )
        except PortableQualificationVerificationErrorV4 as exc:
            rejected = QualificationVerificationServiceResultV4(
                outcome=QualificationVerificationOutcomeV4.REJECTED,
                usable=False,
                failure_code=exc.code,
                failure_detail=exc.detail,
                bundle_id=None,
                bundle_sha256=None,
                checkpoint_v4_id=None,
                checkpoint_v4_signer_key_id=None,
                previous_trust_state=previous,
                next_trust_state=None,
                verified_at=verified_at,
            )
            rejected.validate()
            return rejected

        next_state = QualificationTrustStateV4(
            profile_event_count=result.current_profile_event_count,
            profile_event_head_sha256=result.current_profile_event_head_sha256,
            transparency_tree_size=result.current_transparency_tree_size,
            transparency_root_sha256=result.current_transparency_root_sha256,
            checkpoint_v4_sha256=result.checkpoint_v4_sha256,
        )
        next_state.validate()
        accepted = QualificationVerificationServiceResultV4(
            outcome=QualificationVerificationOutcomeV4.VERIFIED,
            usable=result.usable,
            failure_code=None,
            failure_detail=None,
            bundle_id=result.bundle_id,
            bundle_sha256=result.bundle_sha256,
            checkpoint_v4_id=result.checkpoint_v4_id,
            checkpoint_v4_signer_key_id=result.checkpoint_v4_signer_key_id,
            previous_trust_state=previous,
            next_trust_state=next_state,
            verified_at=result.verified_at,
        )
        accepted.validate()
        return accepted


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
