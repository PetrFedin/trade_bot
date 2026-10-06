from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from app.qualification.evidence_registry import EvidenceVerificationStatus
from app.qualification.portable_verification_v3 import (
    PortableVerificationFailureCodeV3,
)
from app.qualification.profile_registry import QualificationProfileStatus
from app.qualification.verification_service_v3 import (
    QualificationVerificationOutcomeV3,
    QualificationVerificationRequestV3,
    QualificationVerificationServiceV3,
)
from tests.helpers_v108 import NOW
from tests.test_qualification_portable_verification_v3 import bundle_v3


def service(root) -> QualificationVerificationServiceV3:
    return QualificationVerificationServiceV3(
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
    )


def request(bundle, *, seconds: int = 8) -> QualificationVerificationRequestV3:
    return QualificationVerificationRequestV3(
        bundle=bundle,
        previous_keyring_generation=0,
        observed_at=NOW + timedelta(seconds=seconds),
    )


def test_service_v3_returns_verified_and_usable_for_active_chain() -> None:
    bundle, _, _, _, root, _, _, _ = bundle_v3()

    result = service(root).verify(request(bundle))

    assert result.outcome is QualificationVerificationOutcomeV3.VERIFIED
    assert result.usable
    assert result.evidence_lifecycle_status is EvidenceVerificationStatus.VALID
    assert result.profile_lifecycle_status is QualificationProfileStatus.ACTIVE
    assert result.failure_code is None
    assert result.bundle_id == bundle.bundle_id
    assert result.payload()["outcome"] == "VERIFIED"


def test_service_v3_maps_untrusted_root_to_stable_failure() -> None:
    bundle, _, _, _, _, _, _, _ = bundle_v3()
    verifier = QualificationVerificationServiceV3(
        trusted_root_public_keys={"other-root": b"x" * 32},
    )

    result = verifier.verify(request(bundle))

    assert result.outcome is QualificationVerificationOutcomeV3.REJECTED
    assert not result.usable
    assert result.failure_code is PortableVerificationFailureCodeV3.KEYRING_REJECTED
    assert result.evidence_lifecycle_status is None
    assert result.profile_lifecycle_status is None


def test_service_v3_maps_profile_proof_tamper_to_stable_failure() -> None:
    bundle, _, _, _, root, _, _, _ = bundle_v3()
    forged_record = replace(
        bundle.profile_state_proof.record,
        status=QualificationProfileStatus.REVOKED,
        lifecycle_reason="FORGED_POLICY_REVOKE",
    )
    tampered = replace(
        bundle,
        profile_state_proof=replace(
            bundle.profile_state_proof,
            record=forged_record,
        ),
    )

    result = service(root).verify(request(tampered))

    assert result.outcome is QualificationVerificationOutcomeV3.REJECTED
    assert (
        result.failure_code
        is PortableVerificationFailureCodeV3.PROFILE_STATE_PROOF_INVALID
    )


def test_service_v3_rejects_stale_checkpoint_as_bundle_invalid() -> None:
    bundle, _, profile_registry, _, root, _, _, _ = bundle_v3()
    profile_registry.revoke(
        profile_ref=bundle.profile.profile_ref,
        reason="POLICY_WITHDRAWN",
        observed_at=NOW + timedelta(seconds=9),
    )
    stale = replace(
        bundle,
        profile_state_proof=profile_registry.state_proof(
            profile_ref=bundle.profile.profile_ref
        ),
    )

    result = service(root).verify(request(stale, seconds=10))

    assert result.outcome is QualificationVerificationOutcomeV3.REJECTED
    assert result.failure_code is PortableVerificationFailureCodeV3.BUNDLE_INVALID
    assert not result.usable


def test_service_v3_configuration_is_fail_closed() -> None:
    try:
        QualificationVerificationServiceV3(trusted_root_public_keys={})
    except ValueError as exc:
        assert "trusted roots" in str(exc)
    else:
        raise AssertionError("empty trusted roots must be rejected")

    try:
        QualificationVerificationServiceV3(
            trusted_root_public_keys={"root": b"short"},
        )
    except ValueError as exc:
        assert "32-byte" in str(exc)
    else:
        raise AssertionError("invalid root key length must be rejected")
