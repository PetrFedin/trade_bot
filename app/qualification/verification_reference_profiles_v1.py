from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.qualification.verification_api_contract_v1 import VerificationAPIOperation

_PROFILE_SCHEMA_VERSION = "astra-verification-reference-profile-v1"
_CAPABILITY_SCHEMA_VERSION = "astra-verification-reference-capability-v1"
_CONFORMANCE_SCHEMA_VERSION = "astra-verification-reference-conformance-v1"
_ARTIFACT_TYPE = "QUALIFICATION_VERIFICATION_V4"
_CODEC_SCHEMA_VERSION = "astra-qualification-portable-artifact-codec-v1"
_BUNDLE_SCHEMA_VERSION = "astra-portable-qualification-verification-v4"
_CANONICALIZATION = "ASTRA_CANONICAL_JSON_V1"


class VerificationReferenceInterface(StrEnum):
    OFFLINE_CLI = "OFFLINE_CLI"
    LOCAL_HTTP = "LOCAL_HTTP"
    EMBEDDED = "EMBEDDED"
    SDK_LOCAL_HTTP = "SDK_LOCAL_HTTP"


class VerificationAuthorityPersistence(StrEnum):
    NONE = "NONE"
    PERSISTENT_TRUST_STATE = "PERSISTENT_TRUST_STATE"


class VerificationTrustedRootPolicy(StrEnum):
    EXPLICIT_CONFIGURED_ID = "EXPLICIT_CONFIGURED_ID"


class VerificationReferenceConformanceFailure(StrEnum):
    OPERATION_UNSUPPORTED = "OPERATION_UNSUPPORTED"
    ARTIFACT_TYPE_MISMATCH = "ARTIFACT_TYPE_MISMATCH"
    ARTIFACT_CODEC_SCHEMA_MISMATCH = "ARTIFACT_CODEC_SCHEMA_MISMATCH"
    BUNDLE_SCHEMA_MISMATCH = "BUNDLE_SCHEMA_MISMATCH"
    CANONICALIZATION_MISMATCH = "CANONICALIZATION_MISMATCH"
    TRUSTED_ROOT_SET_ID_REQUIRED = "TRUSTED_ROOT_SET_ID_REQUIRED"
    CLOCK_SKEW_EXCEEDS_PROFILE = "CLOCK_SKEW_EXCEEDS_PROFILE"
    INTERFACE_MISMATCH = "INTERFACE_MISMATCH"
    IDEMPOTENCY_PRESERVATION_REQUIRED = "IDEMPOTENCY_PRESERVATION_REQUIRED"
    TRANSPORT_ONLY_RETRY_REQUIRED = "TRANSPORT_ONLY_RETRY_REQUIRED"
    SEMANTIC_RETRY_FORBIDDEN = "SEMANTIC_RETRY_FORBIDDEN"
    AUTHORITY_PERSISTENCE_MISMATCH = "AUTHORITY_PERSISTENCE_MISMATCH"
    CRASH_RECOVERY_REQUIRED = "CRASH_RECOVERY_REQUIRED"
    CAS_REQUIRED = "CAS_REQUIRED"
    EXTERNAL_NETWORK_FORBIDDEN = "EXTERNAL_NETWORK_FORBIDDEN"


@dataclass(frozen=True)
class VerificationReferenceProfileV1:
    profile_id: str
    version: str
    supported_operations: tuple[VerificationAPIOperation, ...]
    interface: VerificationReferenceInterface
    authority_persistence: VerificationAuthorityPersistence
    max_clock_skew_seconds: int
    require_idempotency_preservation: bool
    require_transport_only_retry: bool
    forbid_semantic_retry: bool
    require_crash_recovery: bool
    require_cas: bool
    allow_external_network: bool
    trusted_root_policy: VerificationTrustedRootPolicy = (
        VerificationTrustedRootPolicy.EXPLICIT_CONFIGURED_ID
    )
    artifact_type: str = _ARTIFACT_TYPE
    artifact_codec_schema_version: str = _CODEC_SCHEMA_VERSION
    bundle_schema_version: str = _BUNDLE_SCHEMA_VERSION
    canonicalization: str = _CANONICALIZATION
    schema_version: str = _PROFILE_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _PROFILE_SCHEMA_VERSION:
            raise ValueError("reference profile schema mismatch")
        if not self.profile_id.strip():
            raise ValueError("profile_id is required")
        if not self.version.strip():
            raise ValueError("version is required")
        if not self.supported_operations:
            raise ValueError("supported_operations cannot be empty")
        if len(set(self.supported_operations)) != len(self.supported_operations):
            raise ValueError("supported_operations must be unique")
        if tuple(sorted(item.value for item in self.supported_operations)) != tuple(
            item.value for item in self.supported_operations
        ):
            raise ValueError("supported_operations must use canonical sorted order")
        if self.max_clock_skew_seconds < 0:
            raise ValueError("max_clock_skew_seconds must be non-negative")
        if self.artifact_type != _ARTIFACT_TYPE:
            raise ValueError("reference profile artifact_type mismatch")
        if self.artifact_codec_schema_version != _CODEC_SCHEMA_VERSION:
            raise ValueError("reference profile artifact codec schema mismatch")
        if self.bundle_schema_version != _BUNDLE_SCHEMA_VERSION:
            raise ValueError("reference profile bundle schema mismatch")
        if self.canonicalization != _CANONICALIZATION:
            raise ValueError("reference profile canonicalization mismatch")
        if self.trusted_root_policy is not VerificationTrustedRootPolicy.EXPLICIT_CONFIGURED_ID:
            raise ValueError("reference profile trusted-root policy mismatch")

        has_advance = (
            VerificationAPIOperation.VERIFY_ADVANCE in self.supported_operations
        )
        if has_advance:
            if not self.require_idempotency_preservation:
                raise ValueError(
                    "mutating reference profile requires idempotency preservation"
                )
            if not self.require_cas:
                raise ValueError("mutating reference profile requires CAS")
        if self.forbid_semantic_retry and not self.require_transport_only_retry:
            raise ValueError(
                "semantic retry prohibition requires transport-only retry discipline"
            )
        if (
            self.authority_persistence
            is VerificationAuthorityPersistence.PERSISTENT_TRUST_STATE
            and not self.require_crash_recovery
        ):
            raise ValueError(
                "persistent authority profile requires crash recovery"
            )

    @property
    def profile_ref(self) -> str:
        return f"{self.profile_id}@{self.version}"

    def unsigned_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "version": self.version,
            "supported_operations": [
                item.value for item in self.supported_operations
            ],
            "artifact_type": self.artifact_type,
            "artifact_codec_schema_version": self.artifact_codec_schema_version,
            "bundle_schema_version": self.bundle_schema_version,
            "canonicalization": self.canonicalization,
            "trusted_root_policy": self.trusted_root_policy.value,
            "max_clock_skew_seconds": self.max_clock_skew_seconds,
            "interface": self.interface.value,
            "require_idempotency_preservation": (
                self.require_idempotency_preservation
            ),
            "require_transport_only_retry": self.require_transport_only_retry,
            "forbid_semantic_retry": self.forbid_semantic_retry,
            "authority_persistence": self.authority_persistence.value,
            "require_crash_recovery": self.require_crash_recovery,
            "require_cas": self.require_cas,
            "allow_external_network": self.allow_external_network,
        }

    @property
    def profile_sha256(self) -> str:
        return _sha256(self.unsigned_payload())

    def payload(self) -> dict[str, object]:
        return {
            **self.unsigned_payload(),
            "profile_ref": self.profile_ref,
            "profile_sha256": self.profile_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.payload())


@dataclass(frozen=True)
class VerificationReferenceCapabilityV1:
    operations: tuple[VerificationAPIOperation, ...]
    artifact_type: str
    artifact_codec_schema_version: str
    bundle_schema_version: str
    canonicalization: str
    trusted_root_set_id: str
    max_clock_skew_seconds: int
    interface: VerificationReferenceInterface
    preserves_idempotency_key: bool
    retries_transport_failures_only: bool
    retries_semantic_results: bool
    authority_persistence: VerificationAuthorityPersistence
    crash_recovery: bool
    cas_enforced: bool
    requires_external_network: bool
    schema_version: str = _CAPABILITY_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _CAPABILITY_SCHEMA_VERSION:
            raise ValueError("reference capability schema mismatch")
        if not self.operations:
            raise ValueError("capability operations cannot be empty")
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("capability operations must be unique")
        if tuple(sorted(item.value for item in self.operations)) != tuple(
            item.value for item in self.operations
        ):
            raise ValueError("capability operations must use canonical sorted order")
        if self.max_clock_skew_seconds < 0:
            raise ValueError("capability max_clock_skew_seconds must be non-negative")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "operations": [item.value for item in self.operations],
            "artifact_type": self.artifact_type,
            "artifact_codec_schema_version": self.artifact_codec_schema_version,
            "bundle_schema_version": self.bundle_schema_version,
            "canonicalization": self.canonicalization,
            "trusted_root_set_id": self.trusted_root_set_id,
            "max_clock_skew_seconds": self.max_clock_skew_seconds,
            "interface": self.interface.value,
            "preserves_idempotency_key": self.preserves_idempotency_key,
            "retries_transport_failures_only": self.retries_transport_failures_only,
            "retries_semantic_results": self.retries_semantic_results,
            "authority_persistence": self.authority_persistence.value,
            "crash_recovery": self.crash_recovery,
            "cas_enforced": self.cas_enforced,
            "requires_external_network": self.requires_external_network,
        }

    @property
    def capability_sha256(self) -> str:
        return _sha256(self.payload())


@dataclass(frozen=True)
class VerificationReferenceConformanceV1:
    profile_ref: str
    profile_sha256: str
    capability_sha256: str
    compatible: bool
    failures: tuple[VerificationReferenceConformanceFailure, ...]
    schema_version: str = _CONFORMANCE_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _CONFORMANCE_SCHEMA_VERSION:
            raise ValueError("reference conformance schema mismatch")
        if not self.profile_ref.strip():
            raise ValueError("reference conformance profile_ref is required")
        _digest(self.profile_sha256, "profile_sha256")
        _digest(self.capability_sha256, "capability_sha256")
        if len(set(self.failures)) != len(self.failures):
            raise ValueError("reference conformance failures must be unique")
        if tuple(sorted(item.value for item in self.failures)) != tuple(
            item.value for item in self.failures
        ):
            raise ValueError("reference conformance failures must be canonically sorted")
        if self.compatible != (not self.failures):
            raise ValueError("reference conformance compatible/failures mismatch")

    def unsigned_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "profile_ref": self.profile_ref,
            "profile_sha256": self.profile_sha256,
            "capability_sha256": self.capability_sha256,
            "compatible": self.compatible,
            "failures": [item.value for item in self.failures],
        }

    @property
    def conformance_sha256(self) -> str:
        return _sha256(self.unsigned_payload())

    def payload(self) -> dict[str, object]:
        return {
            **self.unsigned_payload(),
            "conformance_sha256": self.conformance_sha256,
        }


def evaluate_reference_profile_conformance(
    *,
    profile: VerificationReferenceProfileV1,
    capability: VerificationReferenceCapabilityV1,
) -> VerificationReferenceConformanceV1:
    profile.validate()
    capability.validate()
    failures: set[VerificationReferenceConformanceFailure] = set()

    if not set(capability.operations).issubset(set(profile.supported_operations)):
        failures.add(
            VerificationReferenceConformanceFailure.OPERATION_UNSUPPORTED
        )
    if capability.artifact_type != profile.artifact_type:
        failures.add(
            VerificationReferenceConformanceFailure.ARTIFACT_TYPE_MISMATCH
        )
    if (
        capability.artifact_codec_schema_version
        != profile.artifact_codec_schema_version
    ):
        failures.add(
            VerificationReferenceConformanceFailure.ARTIFACT_CODEC_SCHEMA_MISMATCH
        )
    if capability.bundle_schema_version != profile.bundle_schema_version:
        failures.add(
            VerificationReferenceConformanceFailure.BUNDLE_SCHEMA_MISMATCH
        )
    if capability.canonicalization != profile.canonicalization:
        failures.add(
            VerificationReferenceConformanceFailure.CANONICALIZATION_MISMATCH
        )
    if (
        profile.trusted_root_policy
        is VerificationTrustedRootPolicy.EXPLICIT_CONFIGURED_ID
        and not capability.trusted_root_set_id.strip()
    ):
        failures.add(
            VerificationReferenceConformanceFailure.TRUSTED_ROOT_SET_ID_REQUIRED
        )
    if capability.max_clock_skew_seconds > profile.max_clock_skew_seconds:
        failures.add(
            VerificationReferenceConformanceFailure.CLOCK_SKEW_EXCEEDS_PROFILE
        )
    if capability.interface is not profile.interface:
        failures.add(VerificationReferenceConformanceFailure.INTERFACE_MISMATCH)
    if (
        profile.require_idempotency_preservation
        and not capability.preserves_idempotency_key
    ):
        failures.add(
            VerificationReferenceConformanceFailure.IDEMPOTENCY_PRESERVATION_REQUIRED
        )
    if (
        profile.require_transport_only_retry
        and not capability.retries_transport_failures_only
    ):
        failures.add(
            VerificationReferenceConformanceFailure.TRANSPORT_ONLY_RETRY_REQUIRED
        )
    if profile.forbid_semantic_retry and capability.retries_semantic_results:
        failures.add(
            VerificationReferenceConformanceFailure.SEMANTIC_RETRY_FORBIDDEN
        )
    if capability.authority_persistence is not profile.authority_persistence:
        failures.add(
            VerificationReferenceConformanceFailure.AUTHORITY_PERSISTENCE_MISMATCH
        )
    if profile.require_crash_recovery and not capability.crash_recovery:
        failures.add(
            VerificationReferenceConformanceFailure.CRASH_RECOVERY_REQUIRED
        )
    if profile.require_cas and not capability.cas_enforced:
        failures.add(VerificationReferenceConformanceFailure.CAS_REQUIRED)
    if not profile.allow_external_network and capability.requires_external_network:
        failures.add(
            VerificationReferenceConformanceFailure.EXTERNAL_NETWORK_FORBIDDEN
        )

    ordered = tuple(
        sorted(failures, key=lambda item: item.value)
    )
    result = VerificationReferenceConformanceV1(
        profile_ref=profile.profile_ref,
        profile_sha256=profile.profile_sha256,
        capability_sha256=capability.capability_sha256,
        compatible=not ordered,
        failures=ordered,
    )
    result.validate()
    return result


def decode_reference_profile_json(
    encoded: bytes,
) -> VerificationReferenceProfileV1:
    raw = _strict_canonical_json(encoded)
    required = {
        "schema_version",
        "profile_id",
        "version",
        "supported_operations",
        "artifact_type",
        "artifact_codec_schema_version",
        "bundle_schema_version",
        "canonicalization",
        "trusted_root_policy",
        "max_clock_skew_seconds",
        "interface",
        "require_idempotency_preservation",
        "require_transport_only_retry",
        "forbid_semantic_retry",
        "authority_persistence",
        "require_crash_recovery",
        "require_cas",
        "allow_external_network",
        "profile_ref",
        "profile_sha256",
    }
    if set(raw) != required:
        raise ValueError("reference profile fields mismatch")

    try:
        operations_raw = raw["supported_operations"]
        if not isinstance(operations_raw, list):
            raise ValueError("supported_operations must be an array")
        profile = VerificationReferenceProfileV1(
            profile_id=_string(raw["profile_id"], "profile_id"),
            version=_string(raw["version"], "version"),
            supported_operations=tuple(
                VerificationAPIOperation(_string(item, "supported operation"))
                for item in operations_raw
            ),
            artifact_type=_string(raw["artifact_type"], "artifact_type"),
            artifact_codec_schema_version=_string(
                raw["artifact_codec_schema_version"],
                "artifact_codec_schema_version",
            ),
            bundle_schema_version=_string(
                raw["bundle_schema_version"],
                "bundle_schema_version",
            ),
            canonicalization=_string(
                raw["canonicalization"],
                "canonicalization",
            ),
            trusted_root_policy=VerificationTrustedRootPolicy(
                _string(raw["trusted_root_policy"], "trusted_root_policy")
            ),
            max_clock_skew_seconds=_integer(
                raw["max_clock_skew_seconds"],
                "max_clock_skew_seconds",
            ),
            interface=VerificationReferenceInterface(
                _string(raw["interface"], "interface")
            ),
            require_idempotency_preservation=_boolean(
                raw["require_idempotency_preservation"],
                "require_idempotency_preservation",
            ),
            require_transport_only_retry=_boolean(
                raw["require_transport_only_retry"],
                "require_transport_only_retry",
            ),
            forbid_semantic_retry=_boolean(
                raw["forbid_semantic_retry"],
                "forbid_semantic_retry",
            ),
            authority_persistence=VerificationAuthorityPersistence(
                _string(raw["authority_persistence"], "authority_persistence")
            ),
            require_crash_recovery=_boolean(
                raw["require_crash_recovery"],
                "require_crash_recovery",
            ),
            require_cas=_boolean(raw["require_cas"], "require_cas"),
            allow_external_network=_boolean(
                raw["allow_external_network"],
                "allow_external_network",
            ),
            schema_version=_string(raw["schema_version"], "schema_version"),
        )
        profile.validate()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid reference profile: {exc}") from exc

    if _string(raw["profile_ref"], "profile_ref") != profile.profile_ref:
        raise ValueError("reference profile ref mismatch")
    if _string(raw["profile_sha256"], "profile_sha256") != profile.profile_sha256:
        raise ValueError("reference profile digest mismatch")
    return profile


def canonical_reference_profiles_v1() -> tuple[VerificationReferenceProfileV1, ...]:
    read_only_ops = tuple(
        sorted(
            (
                VerificationAPIOperation.AUTHORITY_STATUS,
                VerificationAPIOperation.VERIFY_READ_ONLY,
            ),
            key=lambda item: item.value,
        )
    )
    full_ops = tuple(
        sorted(
            (
                VerificationAPIOperation.AUTHORITY_STATUS,
                VerificationAPIOperation.VERIFY_ADVANCE,
                VerificationAPIOperation.VERIFY_READ_ONLY,
            ),
            key=lambda item: item.value,
        )
    )
    profiles = (
        VerificationReferenceProfileV1(
            profile_id="offline-institutional-verifier",
            version="1.0",
            supported_operations=read_only_ops,
            interface=VerificationReferenceInterface.OFFLINE_CLI,
            authority_persistence=VerificationAuthorityPersistence.NONE,
            max_clock_skew_seconds=5,
            require_idempotency_preservation=False,
            require_transport_only_retry=False,
            forbid_semantic_retry=False,
            require_crash_recovery=False,
            require_cas=False,
            allow_external_network=False,
        ),
        VerificationReferenceProfileV1(
            profile_id="local-http-institutional-verifier",
            version="1.0",
            supported_operations=full_ops,
            interface=VerificationReferenceInterface.LOCAL_HTTP,
            authority_persistence=(
                VerificationAuthorityPersistence.PERSISTENT_TRUST_STATE
            ),
            max_clock_skew_seconds=5,
            require_idempotency_preservation=True,
            require_transport_only_retry=True,
            forbid_semantic_retry=True,
            require_crash_recovery=True,
            require_cas=True,
            allow_external_network=False,
        ),
        VerificationReferenceProfileV1(
            profile_id="embedded-oem-verifier",
            version="1.0",
            supported_operations=full_ops,
            interface=VerificationReferenceInterface.EMBEDDED,
            authority_persistence=(
                VerificationAuthorityPersistence.PERSISTENT_TRUST_STATE
            ),
            max_clock_skew_seconds=5,
            require_idempotency_preservation=True,
            require_transport_only_retry=True,
            forbid_semantic_retry=True,
            require_crash_recovery=True,
            require_cas=True,
            allow_external_network=False,
        ),
        VerificationReferenceProfileV1(
            profile_id="read-only-auditor",
            version="1.0",
            supported_operations=read_only_ops,
            interface=VerificationReferenceInterface.SDK_LOCAL_HTTP,
            authority_persistence=(
                VerificationAuthorityPersistence.PERSISTENT_TRUST_STATE
            ),
            max_clock_skew_seconds=5,
            require_idempotency_preservation=False,
            require_transport_only_retry=True,
            forbid_semantic_retry=True,
            require_crash_recovery=True,
            require_cas=False,
            allow_external_network=False,
        ),
        VerificationReferenceProfileV1(
            profile_id="stateful-authority-operator",
            version="1.0",
            supported_operations=full_ops,
            interface=VerificationReferenceInterface.SDK_LOCAL_HTTP,
            authority_persistence=(
                VerificationAuthorityPersistence.PERSISTENT_TRUST_STATE
            ),
            max_clock_skew_seconds=5,
            require_idempotency_preservation=True,
            require_transport_only_retry=True,
            forbid_semantic_retry=True,
            require_crash_recovery=True,
            require_cas=True,
            allow_external_network=False,
        ),
    )
    for profile in profiles:
        profile.validate()
    return profiles


def _strict_canonical_json(encoded: bytes) -> dict[str, object]:
    if not encoded:
        raise ValueError("reference profile is empty")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise ValueError("reference profile UTF-8 BOM is forbidden")
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("reference profile is not valid UTF-8") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except ValueError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("reference profile JSON is invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError("reference profile root must be an object")
    if canonical_json_bytes(raw) != encoded:
        raise ValueError("reference profile JSON is not canonical")
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate reference profile JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> NoReturn:
    raise ValueError(f"floating-point reference profile JSON is forbidden: {value}")


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite reference profile JSON is forbidden: {value}")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _sha256(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized
