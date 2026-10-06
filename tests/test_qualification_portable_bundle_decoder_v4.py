from __future__ import annotations

import hashlib
from types import MappingProxyType

import pytest

from app.qualification.portable_artifact_codec import (
    DecodedQualificationPortableArtifact,
    canonical_json_bytes,
    decode_portable_qualification_artifact_json,
    encode_portable_qualification_artifact_json,
)
from app.qualification.portable_bundle_decoder_v4 import (
    QualificationBundleDecodeError,
    decode_typed_portable_qualification_bundle_v4,
)
from app.qualification.verification_service_v4 import QualificationTrustStateV4
from tests.test_qualification_portable_verification_v4 import bundle_v4


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _plain(value: object) -> object:
    if isinstance(value, MappingProxyType):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _genesis_state(bundle) -> QualificationTrustStateV4:
    return QualificationTrustStateV4(
        profile_event_count=0,
        profile_event_head_sha256="0" * 64,
        transparency_tree_size=bundle.base_v3.transparency_head.tree_size,
        transparency_root_sha256=bundle.base_v3.transparency_head.root_sha256,
        checkpoint_v4_sha256="0" * 64,
    )


def _decoded_artifact() -> DecodedQualificationPortableArtifact:
    bundle, _ = bundle_v4()
    encoded = encode_portable_qualification_artifact_json(
        bundle=bundle,
        trusted_state=_genesis_state(bundle),
        previous_keyring_generation=0,
    )
    return decode_portable_qualification_artifact_json(encoded)


def _mutated_artifact(mutator) -> DecodedQualificationPortableArtifact:
    decoded = _decoded_artifact()
    bundle_payload = _plain(decoded.bundle_payload)
    assert isinstance(bundle_payload, dict)
    mutator(bundle_payload)

    bundle_sha = _sha(bundle_payload)
    bundle_id = f"qverifyv4_{bundle_sha[:24]}"
    state_sha = _sha(decoded.trusted_state.payload())
    unsigned = {
        "codec_schema_version": decoded.codec_schema_version,
        "artifact_type": decoded.artifact_type,
        "canonicalization": decoded.canonicalization,
        "bundle_id": bundle_id,
        "bundle_sha256": bundle_sha,
        "bundle": bundle_payload,
        "trusted_state": decoded.trusted_state.payload(),
        "trusted_state_sha256": state_sha,
        "previous_keyring_generation": decoded.previous_keyring_generation,
    }
    artifact_sha = _sha(unsigned)
    return DecodedQualificationPortableArtifact(
        artifact_id=f"qartifact_{artifact_sha[:24]}",
        artifact_sha256=artifact_sha,
        bundle_id=bundle_id,
        bundle_sha256=bundle_sha,
        bundle_payload=bundle_payload,
        trusted_state=decoded.trusted_state,
        trusted_state_sha256=state_sha,
        previous_keyring_generation=decoded.previous_keyring_generation,
    )


def test_typed_decoder_roundtrip_matches_exact_canonical_bundle_payload() -> None:
    decoded = _decoded_artifact()

    bundle = decode_typed_portable_qualification_bundle_v4(decoded)

    assert bundle.bundle_id == decoded.bundle_id
    assert bundle.bundle_sha256 == decoded.bundle_sha256
    assert canonical_json_bytes(bundle.payload()) == canonical_json_bytes(
        decoded.bundle_payload
    )


def test_typed_decoder_rejects_unknown_nested_field() -> None:
    artifact = _mutated_artifact(
        lambda payload: payload["base_v3"]["profile"].__setitem__(
            "unexpected",
            "field",
        )
    )

    with pytest.raises(QualificationBundleDecodeError, match="fields mismatch"):
        decode_typed_portable_qualification_bundle_v4(artifact)


def test_typed_decoder_rejects_missing_nested_field() -> None:
    artifact = _mutated_artifact(
        lambda payload: payload["base_v3"]["manifest"].pop("provider")
    )

    with pytest.raises(QualificationBundleDecodeError, match="fields mismatch"):
        decode_typed_portable_qualification_bundle_v4(artifact)


def test_typed_decoder_rejects_wrong_scalar_type() -> None:
    artifact = _mutated_artifact(
        lambda payload: payload["base_v3"]["manifest"].__setitem__(
            "interval_seconds",
            "60",
        )
    )

    with pytest.raises(QualificationBundleDecodeError, match="must be an integer"):
        decode_typed_portable_qualification_bundle_v4(artifact)


def test_typed_decoder_rejects_timezone_naive_datetime() -> None:
    artifact = _mutated_artifact(
        lambda payload: payload["base_v3"]["profile"].__setitem__(
            "created_at",
            "2026-10-06T12:00:00",
        )
    )

    with pytest.raises(QualificationBundleDecodeError, match="timezone-aware"):
        decode_typed_portable_qualification_bundle_v4(artifact)


def test_typed_decoder_rejects_tampered_derived_manifest_identity() -> None:
    artifact = _mutated_artifact(
        lambda payload: payload["base_v3"]["manifest"].__setitem__(
            "manifest_id",
            "qmanifest_" + "f" * 24,
        )
    )

    with pytest.raises(
        QualificationBundleDecodeError,
        match="canonical roundtrip mismatch",
    ):
        decode_typed_portable_qualification_bundle_v4(artifact)


def test_typed_decoder_rejects_tampered_profile_state_identity_copy() -> None:
    artifact = _mutated_artifact(
        lambda payload: payload["base_v3"]["profile_state_proof"]["record"].__setitem__(
            "profile_sha256",
            "f" * 64,
        )
    )

    with pytest.raises(
        QualificationBundleDecodeError,
        match="canonical roundtrip mismatch",
    ):
        decode_typed_portable_qualification_bundle_v4(artifact)


def test_typed_decoder_rejects_invalid_signature_encoding_via_domain_validation() -> None:
    artifact = _mutated_artifact(
        lambda payload: payload["signed_trust_checkpoint_v4"]["signature"].__setitem__(
            "signature_b64",
            "not-base64",
        )
    )

    with pytest.raises(
        QualificationBundleDecodeError,
        match="typed portable bundle validation failed",
    ):
        decode_typed_portable_qualification_bundle_v4(artifact)
