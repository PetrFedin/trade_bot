from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import NoReturn

from app.qualification.portable_verification_v4 import (
    PortableQualificationVerificationBundleV4,
)
from app.qualification.verification_service_v4 import QualificationTrustStateV4

_CODEC_SCHEMA_VERSION = "astra-qualification-portable-artifact-codec-v1"
_ARTIFACT_TYPE = "QUALIFICATION_VERIFICATION_V4"
_CANONICALIZATION = "ASTRA_CANONICAL_JSON_V1"
_BUNDLE_SCHEMA_VERSION = "astra-portable-qualification-verification-v4"
_DEFAULT_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


class QualificationArtifactCodecError(ValueError):
    pass


@dataclass(frozen=True)
class DecodedQualificationPortableArtifact:
    artifact_id: str
    artifact_sha256: str
    bundle_id: str
    bundle_sha256: str
    bundle_payload: Mapping[str, object]
    trusted_state: QualificationTrustStateV4
    trusted_state_sha256: str
    previous_keyring_generation: int
    codec_schema_version: str = _CODEC_SCHEMA_VERSION
    artifact_type: str = _ARTIFACT_TYPE
    canonicalization: str = _CANONICALIZATION

    def validate(self) -> None:
        if self.codec_schema_version != _CODEC_SCHEMA_VERSION:
            raise QualificationArtifactCodecError("portable artifact codec schema mismatch")
        if self.artifact_type != _ARTIFACT_TYPE:
            raise QualificationArtifactCodecError("portable artifact type mismatch")
        if self.canonicalization != _CANONICALIZATION:
            raise QualificationArtifactCodecError("portable artifact canonicalization mismatch")
        if self.previous_keyring_generation < 0:
            raise QualificationArtifactCodecError(
                "previous_keyring_generation must be non-negative"
            )
        _digest(self.artifact_sha256, "artifact_sha256")
        _digest(self.bundle_sha256, "bundle_sha256")
        _digest(self.trusted_state_sha256, "trusted_state_sha256")
        self.trusted_state.validate()

        bundle_schema = self.bundle_payload.get("schema_version")
        if bundle_schema != _BUNDLE_SCHEMA_VERSION:
            raise QualificationArtifactCodecError("portable artifact bundle schema mismatch")
        computed_bundle_sha = _sha256_json(self.bundle_payload)
        if computed_bundle_sha != self.bundle_sha256:
            raise QualificationArtifactCodecError("portable artifact bundle digest mismatch")
        expected_bundle_id = f"qverifyv4_{self.bundle_sha256[:24]}"
        if self.bundle_id != expected_bundle_id:
            raise QualificationArtifactCodecError("portable artifact bundle id mismatch")

        computed_state_sha = _sha256_json(self.trusted_state.payload())
        if computed_state_sha != self.trusted_state_sha256:
            raise QualificationArtifactCodecError(
                "portable artifact trusted state digest mismatch"
            )

        unsigned = _unsigned_artifact_payload(
            bundle_id=self.bundle_id,
            bundle_sha256=self.bundle_sha256,
            bundle_payload=self.bundle_payload,
            trusted_state=self.trusted_state,
            trusted_state_sha256=self.trusted_state_sha256,
            previous_keyring_generation=self.previous_keyring_generation,
        )
        computed_artifact_sha = _sha256_json(unsigned)
        if computed_artifact_sha != self.artifact_sha256:
            raise QualificationArtifactCodecError("portable artifact digest mismatch")
        expected_artifact_id = f"qartifact_{self.artifact_sha256[:24]}"
        if self.artifact_id != expected_artifact_id:
            raise QualificationArtifactCodecError("portable artifact id mismatch")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            **_unsigned_artifact_payload(
                bundle_id=self.bundle_id,
                bundle_sha256=self.bundle_sha256,
                bundle_payload=self.bundle_payload,
                trusted_state=self.trusted_state,
                trusted_state_sha256=self.trusted_state_sha256,
                previous_keyring_generation=self.previous_keyring_generation,
            ),
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
        }


def encode_portable_qualification_artifact_json(
    *,
    bundle: PortableQualificationVerificationBundleV4,
    trusted_state: QualificationTrustStateV4,
    previous_keyring_generation: int,
) -> bytes:
    if previous_keyring_generation < 0:
        raise QualificationArtifactCodecError(
            "previous_keyring_generation must be non-negative"
        )
    bundle.validate()
    trusted_state.validate()

    bundle_payload = bundle.payload()
    bundle_sha256 = _sha256_json(bundle_payload)
    if bundle_sha256 != bundle.bundle_sha256:
        raise QualificationArtifactCodecError(
            "portable artifact bundle canonical digest mismatch"
        )
    bundle_id = bundle.bundle_id
    if bundle_id != f"qverifyv4_{bundle_sha256[:24]}":
        raise QualificationArtifactCodecError(
            "portable artifact bundle canonical id mismatch"
        )

    trusted_state_sha256 = _sha256_json(trusted_state.payload())
    unsigned = _unsigned_artifact_payload(
        bundle_id=bundle_id,
        bundle_sha256=bundle_sha256,
        bundle_payload=bundle_payload,
        trusted_state=trusted_state,
        trusted_state_sha256=trusted_state_sha256,
        previous_keyring_generation=previous_keyring_generation,
    )
    artifact_sha256 = _sha256_json(unsigned)
    payload = {
        **unsigned,
        "artifact_id": f"qartifact_{artifact_sha256[:24]}",
        "artifact_sha256": artifact_sha256,
    }
    return canonical_json_bytes(payload)


def decode_portable_qualification_artifact_json(
    encoded: bytes,
    *,
    max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES,
) -> DecodedQualificationPortableArtifact:
    if max_artifact_bytes <= 0:
        raise QualificationArtifactCodecError("max_artifact_bytes must be positive")
    if not encoded:
        raise QualificationArtifactCodecError("portable artifact is empty")
    if len(encoded) > max_artifact_bytes:
        raise QualificationArtifactCodecError("portable artifact exceeds size limit")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise QualificationArtifactCodecError("portable artifact UTF-8 BOM is not allowed")

    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise QualificationArtifactCodecError(
            "portable artifact is not valid UTF-8"
        ) from exc

    try:
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except QualificationArtifactCodecError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise QualificationArtifactCodecError("portable artifact JSON is invalid") from exc

    if not isinstance(raw, dict):
        raise QualificationArtifactCodecError("portable artifact root must be an object")
    _validate_json_value(raw)

    canonical = canonical_json_bytes(raw)
    if canonical != encoded:
        raise QualificationArtifactCodecError(
            "portable artifact JSON is not canonical"
        )

    required_keys = {
        "codec_schema_version",
        "artifact_type",
        "canonicalization",
        "bundle_id",
        "bundle_sha256",
        "bundle",
        "trusted_state",
        "trusted_state_sha256",
        "previous_keyring_generation",
        "artifact_id",
        "artifact_sha256",
    }
    if set(raw) != required_keys:
        raise QualificationArtifactCodecError(
            "portable artifact top-level fields mismatch"
        )

    bundle_payload = raw["bundle"]
    state_payload = raw["trusted_state"]
    if not isinstance(bundle_payload, dict):
        raise QualificationArtifactCodecError("portable artifact bundle must be an object")
    if not isinstance(state_payload, dict):
        raise QualificationArtifactCodecError(
            "portable artifact trusted_state must be an object"
        )

    trusted_state = _decode_trust_state(state_payload)
    artifact = DecodedQualificationPortableArtifact(
        artifact_id=_required_string(raw, "artifact_id"),
        artifact_sha256=_required_string(raw, "artifact_sha256"),
        bundle_id=_required_string(raw, "bundle_id"),
        bundle_sha256=_required_string(raw, "bundle_sha256"),
        bundle_payload=_freeze_mapping(bundle_payload),
        trusted_state=trusted_state,
        trusted_state_sha256=_required_string(raw, "trusted_state_sha256"),
        previous_keyring_generation=_required_int(raw, "previous_keyring_generation"),
        codec_schema_version=_required_string(raw, "codec_schema_version"),
        artifact_type=_required_string(raw, "artifact_type"),
        canonicalization=_required_string(raw, "canonicalization"),
    )
    artifact.validate()
    return artifact


def canonical_json_bytes(value: object) -> bytes:
    _validate_json_value(value)
    normalized = _plain_json(value)
    try:
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise QualificationArtifactCodecError(
            "value cannot be encoded as ASTRA canonical JSON"
        ) from exc


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _plain_json(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple | list):
        return [_plain_json(item) for item in value]
    return value


def _unsigned_artifact_payload(
    *,
    bundle_id: str,
    bundle_sha256: str,
    bundle_payload: Mapping[str, object],
    trusted_state: QualificationTrustStateV4,
    trusted_state_sha256: str,
    previous_keyring_generation: int,
) -> dict[str, object]:
    return {
        "codec_schema_version": _CODEC_SCHEMA_VERSION,
        "artifact_type": _ARTIFACT_TYPE,
        "canonicalization": _CANONICALIZATION,
        "bundle_id": bundle_id,
        "bundle_sha256": bundle_sha256,
        "bundle": dict(bundle_payload),
        "trusted_state": trusted_state.payload(),
        "trusted_state_sha256": trusted_state_sha256,
        "previous_keyring_generation": previous_keyring_generation,
    }


def _decode_trust_state(payload: dict[str, object]) -> QualificationTrustStateV4:
    expected = {
        "profile_event_count",
        "profile_event_head_sha256",
        "transparency_tree_size",
        "transparency_root_sha256",
        "checkpoint_v4_sha256",
    }
    if set(payload) != expected:
        raise QualificationArtifactCodecError(
            "portable artifact trusted_state fields mismatch"
        )
    state = QualificationTrustStateV4(
        profile_event_count=_required_int(payload, "profile_event_count"),
        profile_event_head_sha256=_required_string(
            payload,
            "profile_event_head_sha256",
        ),
        transparency_tree_size=_required_int(payload, "transparency_tree_size"),
        transparency_root_sha256=_required_string(
            payload,
            "transparency_root_sha256",
        ),
        checkpoint_v4_sha256=_required_string(payload, "checkpoint_v4_sha256"),
    )
    state.validate()
    return state


def _validate_json_value(value: object, *, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise QualificationArtifactCodecError(
            f"floating-point values are forbidden in canonical JSON at {path}"
        )
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise QualificationArtifactCodecError(
                    f"canonical JSON object key must be a string at {path}"
                )
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise QualificationArtifactCodecError(
        f"unsupported canonical JSON value at {path}: {type(value).__name__}"
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationArtifactCodecError(
                f"duplicate JSON object key: {key}"
            )
        result[key] = value
    return result


def _reject_float(value: str) -> NoReturn:
    raise QualificationArtifactCodecError(
        f"floating-point JSON number is forbidden: {value}"
    )


def _reject_constant(value: str) -> NoReturn:
    raise QualificationArtifactCodecError(f"non-finite JSON number is forbidden: {value}")


def _required_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise QualificationArtifactCodecError(f"{name} must be a non-empty string")
    return value


def _required_int(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise QualificationArtifactCodecError(f"{name} must be an integer")
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {
            key: _freeze_json(item)
            for key, item in value.items()
        }
    )


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise QualificationArtifactCodecError(f"{name} must be a sha256 digest")
    return normalized
