from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.qualification.adapter_conformance import AdapterConformanceReport
from app.qualification.profile_registry import QualificationProfile
from app.qualification.qualification_manifest import QualificationManifest

_SCHEMA_VERSION = "astra-profile-bound-qualification-manifest-v1"


@dataclass(frozen=True)
class ProfileBoundQualificationManifest:
    manifest_id: str
    manifest_sha256: str
    profile_id: str
    profile_version: str
    profile_sha256: str
    scope: str
    environment: str
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("profile-bound qualification manifest schema mismatch")
        for name, value in (
            ("manifest_id", self.manifest_id),
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
            ("scope", self.scope),
            ("environment", self.environment),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        for name, value in (
            ("manifest_sha256", self.manifest_sha256),
            ("profile_sha256", self.profile_sha256),
        ):
            _digest(value, name)

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_sha256": self.profile_sha256,
            "scope": self.scope,
            "environment": self.environment,
        }

    @property
    def binding_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def binding_id(self) -> str:
        return f"qbinding_{self.binding_sha256[:24]}"


def bind_manifest_to_profile(
    *,
    manifest: QualificationManifest,
    profile: QualificationProfile,
    adapter_conformance: AdapterConformanceReport,
) -> ProfileBoundQualificationManifest:
    manifest.validate()
    profile.validate()
    adapter_conformance.validate()

    if manifest.profile_id != profile.profile_id:
        raise ValueError("qualification profile binding profile_id mismatch")
    if manifest.profile_version != profile.version:
        raise ValueError("qualification profile binding profile_version mismatch")
    if manifest.scope != profile.scope:
        raise ValueError("qualification profile binding scope mismatch")
    if manifest.environment not in profile.allowed_environments:
        raise ValueError("qualification profile binding environment is not allowed")
    if adapter_conformance.profile_id != profile.profile_id:
        raise ValueError("qualification profile binding adapter profile_id mismatch")
    if adapter_conformance.profile_version != profile.version:
        raise ValueError("qualification profile binding adapter profile_version mismatch")
    if adapter_conformance.evidence_sha256 != manifest.adapter_conformance_sha256:
        raise ValueError("qualification profile binding adapter evidence mismatch")

    check_ids = {check.check_id for check in adapter_conformance.checks}
    missing_checks = set(profile.required_checks) - check_ids
    if missing_checks:
        raise ValueError(
            "qualification profile binding missing required checks: "
            + ",".join(sorted(missing_checks))
        )

    missing_assertions = set(profile.required_assertions) - set(manifest.assertions)
    if missing_assertions:
        raise ValueError(
            "qualification profile binding missing required assertions: "
            + ",".join(sorted(missing_assertions))
        )

    missing_limitations = set(profile.required_limitations) - set(manifest.limitations)
    if missing_limitations:
        raise ValueError(
            "qualification profile binding missing required limitations: "
            + ",".join(sorted(missing_limitations))
        )

    bound = ProfileBoundQualificationManifest(
        manifest_id=manifest.manifest_id,
        manifest_sha256=manifest.manifest_sha256,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        profile_sha256=profile.profile_sha256,
        scope=profile.scope,
        environment=manifest.environment,
    )
    bound.validate()
    return bound


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized
