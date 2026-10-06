from __future__ import annotations

import json

import pytest

from app.qualification.portable_artifact_codec import (
    QualificationArtifactCodecError,
    canonical_json_bytes,
    decode_portable_qualification_artifact_json,
    encode_portable_qualification_artifact_json,
)
from app.qualification.verification_service_v4 import QualificationTrustStateV4
from tests.test_qualification_portable_verification_v4 import bundle_v4


def genesis_state(bundle) -> QualificationTrustStateV4:
    return QualificationTrustStateV4(
        profile_event_count=0,
        profile_event_head_sha256="0" * 64,
        transparency_tree_size=bundle.base_v3.transparency_head.tree_size,
        transparency_root_sha256=bundle.base_v3.transparency_head.root_sha256,
        checkpoint_v4_sha256="0" * 64,
    )


def encoded_artifact():
    bundle, _ = bundle_v4()
    state = genesis_state(bundle)
    encoded = encode_portable_qualification_artifact_json(
        bundle=bundle,
        trusted_state=state,
        previous_keyring_generation=0,
    )
    return bundle, state, encoded


def test_codec_roundtrip_is_byte_for_byte_deterministic() -> None:
    bundle, state, encoded = encoded_artifact()

    decoded = decode_portable_qualification_artifact_json(encoded)
    reencoded = canonical_json_bytes(decoded.payload())

    assert reencoded == encoded
    assert decoded.bundle_id == bundle.bundle_id
    assert decoded.bundle_sha256 == bundle.bundle_sha256
    assert decoded.trusted_state == state
    assert decoded.previous_keyring_generation == 0
    assert decoded.artifact_id.startswith("qartifact_")
    assert len(decoded.artifact_sha256) == 64


def test_codec_same_input_produces_identical_bytes_and_identity() -> None:
    bundle, state, first = encoded_artifact()
    second = encode_portable_qualification_artifact_json(
        bundle=bundle,
        trusted_state=state,
        previous_keyring_generation=0,
    )

    assert first == second
    first_decoded = decode_portable_qualification_artifact_json(first)
    second_decoded = decode_portable_qualification_artifact_json(second)
    assert first_decoded.artifact_id == second_decoded.artifact_id
    assert first_decoded.artifact_sha256 == second_decoded.artifact_sha256


def test_codec_rejects_noncanonical_whitespace() -> None:
    _, _, encoded = encoded_artifact()
    payload = json.loads(encoded.decode("utf-8"))
    noncanonical = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")

    with pytest.raises(QualificationArtifactCodecError, match="not canonical"):
        decode_portable_qualification_artifact_json(noncanonical)


def test_codec_rejects_duplicate_json_keys() -> None:
    duplicate = (
        b'{"artifact_id":"a","artifact_id":"b","artifact_sha256":"'
        + b"0" * 64
        + b'"}'
    )

    with pytest.raises(QualificationArtifactCodecError, match="duplicate JSON object key"):
        decode_portable_qualification_artifact_json(duplicate)


def test_codec_rejects_floating_point_values() -> None:
    with pytest.raises(QualificationArtifactCodecError, match="floating-point"):
        canonical_json_bytes({"unsafe": 1.25})

    with pytest.raises(QualificationArtifactCodecError, match="floating-point"):
        decode_portable_qualification_artifact_json(b'{"unsafe":1.25}')


def test_codec_rejects_bom_invalid_utf8_and_oversize() -> None:
    _, _, encoded = encoded_artifact()

    with pytest.raises(QualificationArtifactCodecError, match="BOM"):
        decode_portable_qualification_artifact_json(b"\xef\xbb\xbf" + encoded)

    with pytest.raises(QualificationArtifactCodecError, match="valid UTF-8"):
        decode_portable_qualification_artifact_json(b"\xff")

    with pytest.raises(QualificationArtifactCodecError, match="size limit"):
        decode_portable_qualification_artifact_json(
            encoded,
            max_artifact_bytes=len(encoded) - 1,
        )


def test_codec_rejects_tampered_bundle_digest() -> None:
    _, _, encoded = encoded_artifact()
    payload = json.loads(encoded.decode("utf-8"))
    payload["bundle_sha256"] = "f" * 64
    tampered = canonical_json_bytes(payload)

    with pytest.raises(QualificationArtifactCodecError, match="bundle digest mismatch"):
        decode_portable_qualification_artifact_json(tampered)


def test_codec_rejects_tampered_bundle_identity() -> None:
    _, _, encoded = encoded_artifact()
    payload = json.loads(encoded.decode("utf-8"))
    payload["bundle_id"] = "qverifyv4_" + "f" * 24
    tampered = canonical_json_bytes(payload)

    with pytest.raises(QualificationArtifactCodecError, match="bundle id mismatch"):
        decode_portable_qualification_artifact_json(tampered)


def test_codec_rejects_tampered_trust_state_digest() -> None:
    _, _, encoded = encoded_artifact()
    payload = json.loads(encoded.decode("utf-8"))
    payload["trusted_state"]["checkpoint_v4_sha256"] = "f" * 64
    tampered = canonical_json_bytes(payload)

    with pytest.raises(
        QualificationArtifactCodecError,
        match="trusted state digest mismatch",
    ):
        decode_portable_qualification_artifact_json(tampered)


def test_codec_rejects_unknown_top_level_field() -> None:
    _, _, encoded = encoded_artifact()
    payload = json.loads(encoded.decode("utf-8"))
    payload["unexpected"] = "field"
    tampered = canonical_json_bytes(payload)

    with pytest.raises(
        QualificationArtifactCodecError,
        match="top-level fields mismatch",
    ):
        decode_portable_qualification_artifact_json(tampered)


def test_codec_rejects_negative_keyring_generation() -> None:
    bundle, state, _ = encoded_artifact()

    with pytest.raises(QualificationArtifactCodecError, match="non-negative"):
        encode_portable_qualification_artifact_json(
            bundle=bundle,
            trusted_state=state,
            previous_keyring_generation=-1,
        )


def test_codec_rejects_unsupported_non_json_value() -> None:
    with pytest.raises(QualificationArtifactCodecError, match="unsupported"):
        canonical_json_bytes({"unsafe": {1, 2, 3}})
