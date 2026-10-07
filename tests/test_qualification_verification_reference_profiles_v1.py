from __future__ import annotations

from dataclasses import replace

import pytest

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.qualification.verification_api_contract_v1 import VerificationAPIOperation
from app.qualification.verification_reference_profiles_v1 import (
    VerificationAuthorityPersistence,
    VerificationReferenceCapabilityV1,
    VerificationReferenceConformanceFailure,
    VerificationReferenceInterface,
    VerificationReferenceProfileV1,
    canonical_reference_profiles_v1,
    decode_reference_profile_json,
    evaluate_reference_profile_conformance,
)


def _profile(profile_id: str) -> VerificationReferenceProfileV1:
    return next(
        item
        for item in canonical_reference_profiles_v1()
        if item.profile_id == profile_id
    )


def _capability_for(
    profile: VerificationReferenceProfileV1,
) -> VerificationReferenceCapabilityV1:
    return VerificationReferenceCapabilityV1(
        operations=profile.supported_operations,
        artifact_type=profile.artifact_type,
        artifact_codec_schema_version=profile.artifact_codec_schema_version,
        bundle_schema_version=profile.bundle_schema_version,
        canonicalization=profile.canonicalization,
        trusted_root_set_id="institutional-roots-v1",
        max_clock_skew_seconds=profile.max_clock_skew_seconds,
        interface=profile.interface,
        preserves_idempotency_key=profile.require_idempotency_preservation,
        retries_transport_failures_only=profile.require_transport_only_retry,
        retries_semantic_results=False,
        authority_persistence=profile.authority_persistence,
        crash_recovery=profile.require_crash_recovery,
        cas_enforced=profile.require_cas,
        requires_external_network=False,
    )


def test_five_canonical_reference_profiles_have_distinct_stable_identity() -> None:
    first = canonical_reference_profiles_v1()
    second = canonical_reference_profiles_v1()

    assert len(first) == 5
    assert {item.profile_id for item in first} == {
        "offline-institutional-verifier",
        "local-http-institutional-verifier",
        "embedded-oem-verifier",
        "read-only-auditor",
        "stateful-authority-operator",
    }
    assert [item.canonical_bytes() for item in first] == [
        item.canonical_bytes() for item in second
    ]
    assert [item.profile_sha256 for item in first] == [
        item.profile_sha256 for item in second
    ]
    assert len({item.profile_sha256 for item in first}) == 5
    assert len({item.profile_ref for item in first}) == 5


@pytest.mark.parametrize(
    "profile",
    canonical_reference_profiles_v1(),
    ids=lambda item: item.profile_id,
)
def test_canonical_profile_round_trip_is_byte_stable(
    profile: VerificationReferenceProfileV1,
) -> None:
    encoded = profile.canonical_bytes()

    decoded = decode_reference_profile_json(encoded)

    assert decoded == profile
    assert decoded.profile_sha256 == profile.profile_sha256
    assert decoded.canonical_bytes() == encoded


@pytest.mark.parametrize(
    "encoded",
    [
        b"",
        b"\xef\xbb\xbf{}",
        b'{"a":1,"a":2}',
        b'{"value":1.5}',
        b'{"value":NaN}',
        b"[]",
        b'{ "not":"canonical" }',
        b"{",
        b"\xff",
    ],
)
def test_reference_profile_decoder_rejects_ambiguous_json(encoded: bytes) -> None:
    with pytest.raises(ValueError):
        decode_reference_profile_json(encoded)


def test_reference_profile_decoder_rejects_unknown_and_missing_fields() -> None:
    profile = _profile("read-only-auditor")
    payload = profile.payload()
    payload["unknown"] = "forbidden"

    with pytest.raises(ValueError, match="fields mismatch"):
        decode_reference_profile_json(canonical_json_bytes(payload))

    payload = profile.payload()
    del payload["profile_sha256"]
    with pytest.raises(ValueError, match="fields mismatch"):
        decode_reference_profile_json(canonical_json_bytes(payload))


def test_reference_profile_decoder_rejects_ref_and_digest_tampering() -> None:
    profile = _profile("read-only-auditor")
    payload = profile.payload()
    payload["profile_ref"] = "other@1.0"

    with pytest.raises(ValueError, match="ref mismatch"):
        decode_reference_profile_json(canonical_json_bytes(payload))

    payload = profile.payload()
    payload["profile_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="digest mismatch"):
        decode_reference_profile_json(canonical_json_bytes(payload))


def test_reference_profile_validation_requires_canonical_operation_order() -> None:
    profile = _profile("local-http-institutional-verifier")
    reversed_operations = tuple(reversed(profile.supported_operations))

    with pytest.raises(ValueError, match="canonical sorted order"):
        replace(profile, supported_operations=reversed_operations).validate()


def test_mutating_profile_cannot_weaken_idempotency_or_cas() -> None:
    profile = _profile("stateful-authority-operator")

    with pytest.raises(ValueError, match="idempotency preservation"):
        replace(profile, require_idempotency_preservation=False).validate()

    with pytest.raises(ValueError, match="requires CAS"):
        replace(profile, require_cas=False).validate()


def test_persistent_profile_requires_crash_recovery() -> None:
    profile = _profile("read-only-auditor")

    with pytest.raises(ValueError, match="requires crash recovery"):
        replace(profile, require_crash_recovery=False).validate()


@pytest.mark.parametrize(
    "profile",
    canonical_reference_profiles_v1(),
    ids=lambda item: item.profile_id,
)
def test_matching_capability_conforms_deterministically(
    profile: VerificationReferenceProfileV1,
) -> None:
    capability = _capability_for(profile)

    first = evaluate_reference_profile_conformance(
        profile=profile,
        capability=capability,
    )
    second = evaluate_reference_profile_conformance(
        profile=profile,
        capability=capability,
    )

    assert first.compatible is True
    assert first.failures == ()
    assert first.profile_ref == profile.profile_ref
    assert first.profile_sha256 == profile.profile_sha256
    assert first.capability_sha256 == capability.capability_sha256
    assert first.payload() == second.payload()
    assert first.conformance_sha256 == second.conformance_sha256


def test_conformance_reports_all_incompatible_assumptions_in_stable_order() -> None:
    profile = _profile("stateful-authority-operator")
    capability = VerificationReferenceCapabilityV1(
        operations=(
            VerificationAPIOperation.AUTHORITY_STATUS,
            VerificationAPIOperation.VERIFY_READ_ONLY,
        ),
        artifact_type="WRONG",
        artifact_codec_schema_version="wrong-codec",
        bundle_schema_version="wrong-bundle",
        canonicalization="wrong-canonicalization",
        trusted_root_set_id="",
        max_clock_skew_seconds=profile.max_clock_skew_seconds + 1,
        interface=VerificationReferenceInterface.OFFLINE_CLI,
        preserves_idempotency_key=False,
        retries_transport_failures_only=False,
        retries_semantic_results=True,
        authority_persistence=VerificationAuthorityPersistence.NONE,
        crash_recovery=False,
        cas_enforced=False,
        requires_external_network=True,
    )

    result = evaluate_reference_profile_conformance(
        profile=profile,
        capability=capability,
    )

    assert result.compatible is False
    assert result.failures == tuple(
        sorted(
            (
                VerificationReferenceConformanceFailure.ARTIFACT_TYPE_MISMATCH,
                VerificationReferenceConformanceFailure.ARTIFACT_CODEC_SCHEMA_MISMATCH,
                VerificationReferenceConformanceFailure.BUNDLE_SCHEMA_MISMATCH,
                VerificationReferenceConformanceFailure.CANONICALIZATION_MISMATCH,
                VerificationReferenceConformanceFailure.TRUSTED_ROOT_SET_ID_REQUIRED,
                VerificationReferenceConformanceFailure.CLOCK_SKEW_EXCEEDS_PROFILE,
                VerificationReferenceConformanceFailure.INTERFACE_MISMATCH,
                VerificationReferenceConformanceFailure.IDEMPOTENCY_PRESERVATION_REQUIRED,
                VerificationReferenceConformanceFailure.TRANSPORT_ONLY_RETRY_REQUIRED,
                VerificationReferenceConformanceFailure.SEMANTIC_RETRY_FORBIDDEN,
                VerificationReferenceConformanceFailure.AUTHORITY_PERSISTENCE_MISMATCH,
                VerificationReferenceConformanceFailure.CRASH_RECOVERY_REQUIRED,
                VerificationReferenceConformanceFailure.CAS_REQUIRED,
                VerificationReferenceConformanceFailure.EXTERNAL_NETWORK_FORBIDDEN,
            ),
            key=lambda item: item.value,
        )
    )


def test_operation_outside_profile_fails_closed() -> None:
    profile = _profile("read-only-auditor")
    capability = replace(
        _capability_for(profile),
        operations=tuple(
            sorted(
                (
                    *profile.supported_operations,
                    VerificationAPIOperation.VERIFY_ADVANCE,
                ),
                key=lambda item: item.value,
            )
        ),
    )

    result = evaluate_reference_profile_conformance(
        profile=profile,
        capability=capability,
    )

    assert result.failures == (
        VerificationReferenceConformanceFailure.OPERATION_SET_MISMATCH,
    )


def test_read_only_profile_does_not_require_mutating_idempotency_or_cas() -> None:
    profile = _profile("read-only-auditor")
    capability = _capability_for(profile)

    assert capability.preserves_idempotency_key is False
    assert capability.cas_enforced is False
    result = evaluate_reference_profile_conformance(
        profile=profile,
        capability=capability,
    )
    assert result.compatible is True


def test_capability_identity_changes_when_declared_environment_changes() -> None:
    profile = _profile("local-http-institutional-verifier")
    first = _capability_for(profile)
    second = replace(first, trusted_root_set_id="institutional-roots-v2")

    assert first.capability_sha256 != second.capability_sha256


def test_conformance_identity_binds_profile_identity() -> None:
    first_profile = _profile("local-http-institutional-verifier")
    second_profile = _profile("embedded-oem-verifier")
    first_capability = _capability_for(first_profile)
    second_capability = _capability_for(second_profile)

    first = evaluate_reference_profile_conformance(
        profile=first_profile,
        capability=first_capability,
    )
    second = evaluate_reference_profile_conformance(
        profile=second_profile,
        capability=second_capability,
    )

    assert first.profile_sha256 != second.profile_sha256
    assert first.conformance_sha256 != second.conformance_sha256
