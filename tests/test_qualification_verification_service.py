from __future__ import annotations

import base64
from dataclasses import replace
from datetime import timedelta

from app.qualification.evidence_registry import EvidenceVerificationStatus
from app.qualification.portable_verification import PortableVerificationFailureCode
from app.qualification.registry_checkpoint import (
    build_registry_checkpoint,
    sign_registry_checkpoint,
)
from app.qualification.transparency_log import QualificationTransparencyEntry
from app.qualification.verification_service import (
    QualificationVerificationOutcome,
    QualificationVerificationRequest,
    QualificationVerificationService,
)
from tests.helpers_v108 import NOW
from tests.test_qualification_portable_verification import full_bundle


def service(root) -> QualificationVerificationService:
    return QualificationVerificationService(
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
    )


def request(bundle) -> QualificationVerificationRequest:
    return QualificationVerificationRequest(
        bundle=bundle,
        previous_keyring_generation=0,
        observed_at=NOW + timedelta(seconds=7),
    )


def test_verification_service_returns_machine_readable_verified_result() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()

    result = service(root).verify(request(bundle))

    assert result.outcome is QualificationVerificationOutcome.VERIFIED
    assert result.usable
    assert result.lifecycle_status is EvidenceVerificationStatus.VALID
    assert result.failure_code is None
    assert result.failure_detail is None
    assert result.bundle_id == bundle.bundle_id
    assert result.evidence_id == bundle.signed_evidence.evidence_id
    assert result.profile_ref == bundle.profile.profile_ref
    assert result.payload()["outcome"] == "VERIFIED"
    assert result.payload()["usable"] is True


def test_untrusted_root_maps_to_stable_keyring_failure_code() -> None:
    bundle, _, _, _, _, _, _ = full_bundle()
    other = QualificationVerificationService(
        trusted_root_public_keys={"untrusted-root": b"x" * 32},
    )

    result = other.verify(request(bundle))

    assert result.outcome is QualificationVerificationOutcome.REJECTED
    assert not result.usable
    assert result.lifecycle_status is None
    assert result.failure_code is PortableVerificationFailureCode.KEYRING_REJECTED
    assert result.bundle_id is None


def test_bundle_substitution_maps_to_bundle_invalid_code() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()
    tampered = replace(
        bundle,
        manifest=replace(bundle.manifest, organisation_id="OTHER"),
    )

    result = service(root).verify(request(tampered))

    assert result.outcome is QualificationVerificationOutcome.REJECTED
    assert result.failure_code is PortableVerificationFailureCode.BUNDLE_INVALID
    assert not result.usable


def test_evidence_signature_tamper_maps_to_signature_failure_code() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()
    raw = bytearray(base64.b64decode(bundle.signed_evidence.envelope.signature_b64))
    raw[0] ^= 1
    tampered = replace(
        bundle,
        signed_evidence=replace(
            bundle.signed_evidence,
            envelope=replace(
                bundle.signed_evidence.envelope,
                signature_b64=base64.b64encode(bytes(raw)).decode("ascii"),
            ),
        ),
    )

    result = service(root).verify(request(tampered))

    assert result.outcome is QualificationVerificationOutcome.REJECTED
    assert (
        result.failure_code
        is PortableVerificationFailureCode.EVIDENCE_SIGNATURE_REJECTED
    )


def test_state_proof_tamper_maps_to_state_proof_failure_code() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()
    assert bundle.registry_state_proof.audit_path
    tampered = replace(
        bundle,
        registry_state_proof=replace(
            bundle.registry_state_proof,
            audit_path=("f" * 64, *bundle.registry_state_proof.audit_path[1:]),
        ),
    )

    result = service(root).verify(request(tampered))

    assert result.outcome is QualificationVerificationOutcome.REJECTED
    assert (
        result.failure_code
        is PortableVerificationFailureCode.REGISTRY_STATE_PROOF_INVALID
    )


def test_transparency_tamper_maps_to_transparency_failure_code() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()
    assert bundle.transparency_inclusion_proof.audit_path
    tampered = replace(
        bundle,
        transparency_inclusion_proof=replace(
            bundle.transparency_inclusion_proof,
            audit_path=(
                "f" * 64,
                *bundle.transparency_inclusion_proof.audit_path[1:],
            ),
        ),
    )

    result = service(root).verify(request(tampered))

    assert result.outcome is QualificationVerificationOutcome.REJECTED
    assert (
        result.failure_code
        is PortableVerificationFailureCode.TRANSPARENCY_PROOF_INVALID
    )


def test_checkpoint_signature_tamper_maps_to_checkpoint_failure_code() -> None:
    bundle, _, _, root, _, _, _ = full_bundle()
    raw = bytearray(
        base64.b64decode(bundle.signed_registry_checkpoint.envelope.signature_b64)
    )
    raw[0] ^= 1
    tampered = replace(
        bundle,
        signed_registry_checkpoint=replace(
            bundle.signed_registry_checkpoint,
            envelope=replace(
                bundle.signed_registry_checkpoint.envelope,
                signature_b64=base64.b64encode(bytes(raw)).decode("ascii"),
            ),
        ),
    )

    result = service(root).verify(request(tampered))

    assert result.outcome is QualificationVerificationOutcome.REJECTED
    assert (
        result.failure_code
        is PortableVerificationFailureCode.CHECKPOINT_SIGNATURE_REJECTED
    )


def test_revoked_evidence_is_verified_but_not_usable() -> None:
    bundle, registry, log, root, signer, descriptor, keyring = full_bundle()
    evidence_id = bundle.signed_evidence.evidence_id
    registry.revoke(
        evidence_id=evidence_id,
        reason="QUALIFICATION_RETRACTED",
        observed_at=NOW + timedelta(seconds=8),
    )
    log.append(
        entry=QualificationTransparencyEntry(
            entry_type="QUALIFICATION_REVOCATION",
            object_id=f"revocation-{evidence_id}",
            object_sha256=registry.head_sha256,
            subject=bundle.manifest.subject,
            subject_version=bundle.manifest.subject_version,
            profile_id=bundle.manifest.profile_id,
            profile_version=bundle.manifest.profile_version,
            published_at=NOW + timedelta(seconds=9),
        ),
        issued_at=NOW + timedelta(seconds=9),
    )
    decision = registry.verify(
        evidence_id=evidence_id,
        observed_at=NOW + timedelta(seconds=10),
    )
    state_proof = registry.state_proof(evidence_id=evidence_id)
    inclusion = log.inclusion_proof(object_id=evidence_id)
    head = log.latest_head()
    checkpoint = build_registry_checkpoint(
        registry=registry,
        transparency_head=head,
        issued_at=NOW + timedelta(seconds=11),
        previous_checkpoint=bundle.signed_registry_checkpoint.checkpoint,
    )
    signed_checkpoint = sign_registry_checkpoint(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="service-checkpoint-revoked",
        issued_at=NOW + timedelta(seconds=11),
        expires_at=NOW + timedelta(minutes=10),
        nonce="service-checkpoint-revoked-nonce",
    )
    revoked = replace(
        bundle,
        registry_decision=decision,
        registry_state_proof=state_proof,
        transparency_inclusion_proof=inclusion,
        transparency_head=head,
        signed_registry_checkpoint=signed_checkpoint,
    )
    result = service(root).verify(
        QualificationVerificationRequest(
            bundle=revoked,
            previous_keyring_generation=0,
            observed_at=NOW + timedelta(seconds=12),
        )
    )

    assert result.outcome is QualificationVerificationOutcome.VERIFIED
    assert result.lifecycle_status is EvidenceVerificationStatus.REVOKED
    assert not result.usable
    assert result.failure_code is None


def test_verification_service_configuration_is_fail_closed() -> None:
    try:
        QualificationVerificationService(trusted_root_public_keys={})
    except ValueError as exc:
        assert "trusted roots" in str(exc)
    else:
        raise AssertionError("empty trust roots must be rejected")

    try:
        QualificationVerificationService(
            trusted_root_public_keys={"root": b"short"},
        )
    except ValueError as exc:
        assert "32-byte" in str(exc)
    else:
        raise AssertionError("invalid Ed25519 root must be rejected")
