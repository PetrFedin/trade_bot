from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.portable_verification_v4 import (
    PortableVerificationFailureCodeV4,
)
from app.qualification.verification_service_v4 import (
    QualificationTrustStateV4,
    QualificationVerificationOutcomeV4,
    QualificationVerificationRequestV4,
    QualificationVerificationServiceV4,
)
from tests.helpers_v108 import NOW
from tests.test_qualification_portable_verification_v4 import bundle_v4


def genesis_state(bundle) -> QualificationTrustStateV4:
    return QualificationTrustStateV4(
        profile_event_count=0,
        profile_event_head_sha256="0" * 64,
        transparency_tree_size=bundle.base_v3.transparency_head.tree_size,
        transparency_root_sha256=bundle.base_v3.transparency_head.root_sha256,
        checkpoint_v4_sha256="0" * 64,
    )


def service(root) -> QualificationVerificationServiceV4:
    return QualificationVerificationServiceV4(
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
    )


def request(bundle, state) -> QualificationVerificationRequestV4:
    return QualificationVerificationRequestV4(
        bundle=bundle,
        previous_keyring_generation=0,
        trusted_state=state,
        observed_at=NOW + timedelta(seconds=11),
    )


def test_service_v4_verifies_and_advances_trust_state() -> None:
    bundle, root = bundle_v4()
    previous = genesis_state(bundle)

    result = service(root).verify(request(bundle, previous))

    assert result.outcome is QualificationVerificationOutcomeV4.VERIFIED
    assert result.usable
    assert result.failure_code is None
    assert result.previous_trust_state == previous
    assert result.next_trust_state is not None
    assert result.next_trust_state.profile_event_count == (
        bundle.profile_event_delta.current_event_count
    )
    assert result.next_trust_state.profile_event_head_sha256 == (
        bundle.profile_event_delta.current_event_head_sha256
    )
    assert result.next_trust_state.transparency_tree_size == (
        bundle.current_transparency_head.tree_size
    )
    assert result.next_trust_state.transparency_root_sha256 == (
        bundle.current_transparency_head.root_sha256
    )
    assert result.next_trust_state.checkpoint_v4_sha256 == (
        bundle.signed_trust_checkpoint_v4.checkpoint.checkpoint_sha256
    )


def test_service_v4_rejection_never_advances_trust_state() -> None:
    bundle, root = bundle_v4()
    previous = replace(
        genesis_state(bundle),
        checkpoint_v4_sha256="f" * 64,
    )

    result = service(root).verify(request(bundle, previous))

    assert result.outcome is QualificationVerificationOutcomeV4.REJECTED
    assert not result.usable
    assert result.failure_code is PortableVerificationFailureCodeV4.V4_BINDING_MISMATCH
    assert result.previous_trust_state == previous
    assert result.next_trust_state is None


def test_service_v4_maps_transparency_anchor_mismatch() -> None:
    bundle, root = bundle_v4()
    previous = replace(
        genesis_state(bundle),
        transparency_root_sha256="f" * 64,
    )

    result = service(root).verify(request(bundle, previous))

    assert result.outcome is QualificationVerificationOutcomeV4.REJECTED
    assert (
        result.failure_code
        is PortableVerificationFailureCodeV4.TRANSPARENCY_CONTINUITY_REJECTED
    )
    assert result.next_trust_state is None


def test_service_v4_trust_state_validation_is_fail_closed() -> None:
    bundle, _ = bundle_v4()

    with pytest.raises(ValueError, match="profile_event_count"):
        replace(
            genesis_state(bundle),
            profile_event_count=-1,
        ).validate()

    with pytest.raises(ValueError, match="genesis head"):
        replace(
            genesis_state(bundle),
            profile_event_head_sha256="f" * 64,
        ).validate()

    with pytest.raises(ValueError, match="sha256"):
        replace(
            genesis_state(bundle),
            checkpoint_v4_sha256="bad",
        ).validate()


def test_service_v4_configuration_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="trusted roots"):
        QualificationVerificationServiceV4(trusted_root_public_keys={})

    with pytest.raises(ValueError, match="32-byte"):
        QualificationVerificationServiceV4(
            trusted_root_public_keys={"root": b"short"},
        )

    with pytest.raises(ValueError, match="non-negative"):
        QualificationVerificationServiceV4(
            trusted_root_public_keys={"root": b"x" * 32},
            max_clock_skew_seconds=-1,
        )


def test_service_v4_payload_exposes_atomic_trust_state_transition() -> None:
    bundle, root = bundle_v4()
    previous = genesis_state(bundle)

    result = service(root).verify(request(bundle, previous))
    payload = result.payload()

    assert payload["outcome"] == "VERIFIED"
    assert payload["previous_trust_state"] == previous.payload()
    assert payload["next_trust_state"] == result.next_trust_state.payload()
    assert payload["bundle_id"] == bundle.bundle_id
