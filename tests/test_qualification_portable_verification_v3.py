from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.evidence_registry import EvidenceVerificationStatus
from app.qualification.portable_verification_v3 import (
    PortableQualificationVerificationBundleV3,
    PortableQualificationVerificationErrorV3,
    PortableVerificationFailureCodeV3,
    verify_portable_qualification_bundle_v3,
)
from app.qualification.profile_registry import (
    QualificationProfileRegistry,
    QualificationProfileStatus,
)
from app.qualification.trust_checkpoint import (
    build_trust_checkpoint,
    sign_trust_checkpoint,
)
from tests.helpers_v108 import NOW
from tests.test_qualification_portable_verification import full_bundle


def profile_registry_for(profile):
    registry = QualificationProfileRegistry()
    registry.register(profile=profile, observed_at=NOW)
    registry.activate(
        profile_ref=profile.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )
    return registry


def bundle_v3():
    bundle, evidence_registry, log, root, signer, descriptor, keyring = full_bundle()
    profile_registry = profile_registry_for(bundle.profile)
    trust = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=7),
    )
    signed_trust = sign_trust_checkpoint(
        checkpoint=trust,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="portable-v3-trust",
        issued_at=NOW + timedelta(seconds=7),
        expires_at=NOW + timedelta(minutes=10),
        nonce="portable-v3-trust-nonce",
    )
    value = PortableQualificationVerificationBundleV3(
        profile=bundle.profile,
        manifest=bundle.manifest,
        binding=bundle.binding,
        signed_evidence=bundle.signed_evidence,
        registry_decision=bundle.registry_decision,
        evidence_state_proof=bundle.registry_state_proof,
        profile_state_proof=profile_registry.state_proof(
            profile_ref=bundle.profile.profile_ref
        ),
        transparency_entry=bundle.transparency_entry,
        transparency_inclusion_proof=bundle.transparency_inclusion_proof,
        transparency_head=bundle.transparency_head,
        signed_trust_checkpoint=signed_trust,
        keyring_snapshot=bundle.keyring_snapshot,
    )
    value.validate()
    return (
        value,
        evidence_registry,
        profile_registry,
        log,
        root,
        signer,
        descriptor,
        keyring,
    )


def test_portable_v3_verifies_active_evidence_and_active_policy_as_usable() -> None:
    bundle, _, _, _, root, _, _, _ = bundle_v3()

    result = verify_portable_qualification_bundle_v3(
        bundle=bundle,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_keyring_generation=0,
        observed_at=NOW + timedelta(seconds=8),
    )

    assert result.evidence_lifecycle_status is EvidenceVerificationStatus.VALID
    assert result.profile_lifecycle_status is QualificationProfileStatus.ACTIVE
    assert result.usable
    assert result.bundle_id == bundle.bundle_id
    assert result.bundle_id.startswith("qverifyv3_")
    assert result.profile_ref == bundle.profile.profile_ref
    assert result.payload()["usable"] is True


def test_revoked_profile_is_verified_but_not_usable() -> None:
    (
        bundle,
        evidence_registry,
        profile_registry,
        log,
        root,
        signer,
        descriptor,
        keyring,
    ) = bundle_v3()
    profile_registry.revoke(
        profile_ref=bundle.profile.profile_ref,
        reason="POLICY_WITHDRAWN",
        observed_at=NOW + timedelta(seconds=9),
    )
    trust = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=10),
        previous_checkpoint=bundle.signed_trust_checkpoint.checkpoint,
    )
    signed_trust = sign_trust_checkpoint(
        checkpoint=trust,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="portable-v3-revoked-profile",
        issued_at=NOW + timedelta(seconds=10),
        expires_at=NOW + timedelta(minutes=10),
        nonce="portable-v3-revoked-profile-nonce",
    )
    revoked = replace(
        bundle,
        profile_state_proof=profile_registry.state_proof(
            profile_ref=bundle.profile.profile_ref
        ),
        signed_trust_checkpoint=signed_trust,
    )

    result = verify_portable_qualification_bundle_v3(
        bundle=revoked,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_keyring_generation=0,
        observed_at=NOW + timedelta(seconds=11),
    )

    assert result.evidence_lifecycle_status is EvidenceVerificationStatus.VALID
    assert result.profile_lifecycle_status is QualificationProfileStatus.REVOKED
    assert not result.usable


def test_revoked_evidence_is_verified_but_not_usable() -> None:
    (
        bundle,
        evidence_registry,
        profile_registry,
        log,
        root,
        signer,
        descriptor,
        keyring,
    ) = bundle_v3()
    evidence_registry.revoke(
        evidence_id=bundle.signed_evidence.evidence_id,
        reason="EVIDENCE_WITHDRAWN",
        observed_at=NOW + timedelta(seconds=9),
    )
    decision = evidence_registry.verify(
        evidence_id=bundle.signed_evidence.evidence_id,
        observed_at=NOW + timedelta(seconds=10),
    )
    evidence_proof = evidence_registry.state_proof(
        evidence_id=bundle.signed_evidence.evidence_id
    )
    trust = build_trust_checkpoint(
        evidence_registry=evidence_registry,
        profile_registry=profile_registry,
        transparency_head=log.latest_head(),
        issued_at=NOW + timedelta(seconds=10),
        previous_checkpoint=bundle.signed_trust_checkpoint.checkpoint,
    )
    signed_trust = sign_trust_checkpoint(
        checkpoint=trust,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="portable-v3-revoked-evidence",
        issued_at=NOW + timedelta(seconds=10),
        expires_at=NOW + timedelta(minutes=10),
        nonce="portable-v3-revoked-evidence-nonce",
    )
    revoked = replace(
        bundle,
        registry_decision=decision,
        evidence_state_proof=evidence_proof,
        signed_trust_checkpoint=signed_trust,
    )

    result = verify_portable_qualification_bundle_v3(
        bundle=revoked,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_keyring_generation=0,
        observed_at=NOW + timedelta(seconds=11),
    )

    assert result.evidence_lifecycle_status is EvidenceVerificationStatus.REVOKED
    assert result.profile_lifecycle_status is QualificationProfileStatus.ACTIVE
    assert not result.usable


def test_portable_v3_rejects_forged_profile_state_proof() -> None:
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

    with pytest.raises(PortableQualificationVerificationErrorV3) as caught:
        verify_portable_qualification_bundle_v3(
            bundle=tampered,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=8),
        )

    assert caught.value.code is PortableVerificationFailureCodeV3.PROFILE_STATE_PROOF_INVALID


def test_portable_v3_rejects_stale_trust_checkpoint_after_profile_revocation() -> None:
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

    with pytest.raises(PortableQualificationVerificationErrorV3) as caught:
        verify_portable_qualification_bundle_v3(
            bundle=stale,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=10),
        )

    assert caught.value.code is PortableVerificationFailureCodeV3.BUNDLE_INVALID
    assert "trust profile state root mismatch" in caught.value.detail


def test_portable_v3_rejects_untrusted_root() -> None:
    bundle, _, _, _, _, _, _, _ = bundle_v3()

    with pytest.raises(PortableQualificationVerificationErrorV3) as caught:
        verify_portable_qualification_bundle_v3(
            bundle=bundle,
            trusted_root_public_keys={},
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=8),
        )

    assert caught.value.code is PortableVerificationFailureCodeV3.KEYRING_REJECTED
