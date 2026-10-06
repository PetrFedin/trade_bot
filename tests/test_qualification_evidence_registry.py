from __future__ import annotations

import base64
from base64 import b64encode
from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.evidence_registry import (
    EvidenceLifecycleStatus,
    EvidenceVerificationStatus,
    QualificationEvidenceRegistry,
    verify_state_proof,
)
from app.qualification.profile_binding import ProfileBoundQualificationManifest
from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.signed_evidence import sign_qualification_binding
from app.qualification.signing_authority import (
    QualificationKeyringSnapshot,
    QualificationSigningKeyDescriptor,
    verify_qualification_keyring,
)
from app.runtime.signing_authority_v108 import SigningBackendV108
from tests.helpers_v108 import NOW, LocalProviderV108


def manifest(
    *,
    subject_version: str,
    profile_id: str = "ASTRA_BYBIT_PUBLIC_MARKETDATA",
) -> QualificationManifest:
    value = QualificationManifest(
        job_id=f"qjob_{subject_version[:24]}",
        job_result_sha256="1" * 64,
        request_sha256="2" * 64,
        organisation_id="ASTRA_INTERNAL",
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version=subject_version,
        profile_id=profile_id,
        profile_version="1.0.0",
        environment="mainnet-public-readonly",
        corpus_ids=(f"BYBIT:BTCUSDT:{subject_version}",),
        provider_replay_sha256="3" * 64,
        adapter_conformance_sha256="4" * 64,
        marketdata_integrity_sha256="5" * 64,
        continuity_checkpoint_id=f"checkpoint-{subject_version[:8]}",
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        scope="PUBLIC_MARKET_DATA_ONLY",
        assertions=(
            "PROVIDER_COMPLETE_REPLAY_BOUND",
            "ADAPTER_CONFORMANCE_PASS",
            "MARKET_DATA_INTEGRITY_HEALTHY",
            "READ_ONLY_PUBLIC_MARKETDATA_SCOPE",
        ),
        limitations=(
            "EXECUTION_ORDER_SUBMIT_NOT_QUALIFIED",
            "EXECUTION_CANCEL_REPLACE_NOT_QUALIFIED",
            "EXECUTION_FILL_STATUS_MAPPING_NOT_QUALIFIED",
            "ACCOUNT_RECONCILIATION_NOT_QUALIFIED",
            "PROFITABILITY_NOT_PROVEN",
            "REGULATORY_CERTIFICATION_NOT_CLAIMED",
        ),
        issued_at=NOW,
    )
    value.validate()
    return value


def bound(value: QualificationManifest) -> ProfileBoundQualificationManifest:
    result = ProfileBoundQualificationManifest(
        manifest_id=value.manifest_id,
        manifest_sha256=value.manifest_sha256,
        profile_id=value.profile_id,
        profile_version=value.profile_version,
        profile_sha256="6" * 64,
        scope=value.scope,
        environment=value.environment,
    )
    result.validate()
    return result


def authority():
    root = LocalProviderV108.create("qualification-root", SigningBackendV108.HSM)
    signer = LocalProviderV108.create("qualification-key", SigningBackendV108.HSM)
    descriptor = QualificationSigningKeyDescriptor(
        key_id=signer.key_id,
        owner_id="qualification-owner",
        backend="HSM",
        generation=1,
        public_key_b64=b64encode(signer.public_key_bytes()).decode("ascii"),
        not_before=NOW - timedelta(hours=1),
        not_after=NOW + timedelta(hours=1),
    )
    descriptor.validate()
    snapshot = QualificationKeyringSnapshot.sign(
        generation=1,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=30),
        keys=(descriptor,),
        root_provider=root,
    )
    keyring = verify_qualification_keyring(
        snapshot,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_generation=0,
        observed_at=NOW,
    )
    return signer, descriptor, keyring


def evidence(
    *,
    subject_version: str,
    suffix: str,
    profile_id: str = "ASTRA_BYBIT_PUBLIC_MARKETDATA",
):
    signer, descriptor, keyring = authority()
    source = manifest(subject_version=subject_version, profile_id=profile_id)
    binding = bound(source)
    signed = sign_qualification_binding(
        binding=binding,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id=f"qualification-{suffix}",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        nonce=f"qualification-nonce-{suffix}",
    )
    return source, binding, signed, keyring


def register_bundle(
    registry: QualificationEvidenceRegistry,
    bundle,
    *,
    seconds: int,
):
    source, binding, signed, keyring = bundle
    return registry.register(
        manifest=source,
        binding=binding,
        signed_evidence=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=seconds),
    )


def test_registry_performs_crypto_verification_and_persists_signer_metadata() -> None:
    registry = QualificationEvidenceRegistry()
    source, binding, signed, keyring = evidence(
        subject_version="build-0001",
        suffix="one",
    )

    record = registry.register(
        manifest=source,
        binding=binding,
        signed_evidence=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=2),
    )

    assert record.status is EvidenceLifecycleStatus.ACTIVE
    assert record.binding_sha256 == binding.binding_sha256
    assert record.manifest_sha256 == source.manifest_sha256
    assert record.profile_sha256 == binding.profile_sha256
    assert record.signer_key_id == "qualification-key"
    assert record.signer_owner_id == "qualification-owner"
    assert record.signer_key_generation == 1
    assert record.keyring_generation == 1
    assert record.signature_envelope_sha256 == signed.envelope.envelope_sha256
    assert record.cryptographically_verified_at == NOW + timedelta(seconds=2)
    assert registry.event_count == 1
    assert registry.verify_chain() == registry.head_sha256
    assert registry.state_root_sha256 != "0" * 64

    decision = registry.verify(
        evidence_id=signed.evidence_id,
        observed_at=NOW + timedelta(seconds=3),
    )
    assert decision.status is EvidenceVerificationStatus.VALID
    assert decision.reason is None
    assert decision.binding_sha256 == binding.binding_sha256
    assert decision.signer_key_id == "qualification-key"
    assert decision.state_root_sha256 == registry.state_root_sha256


def test_registry_rejects_forged_signature_without_external_verification_object() -> None:
    registry = QualificationEvidenceRegistry()
    source, binding, signed, keyring = evidence(
        subject_version="build-0002",
        suffix="forged",
    )
    raw = bytearray(base64.b64decode(signed.envelope.signature_b64))
    raw[0] ^= 1
    forged = replace(
        signed,
        envelope=replace(
            signed.envelope,
            signature_b64=base64.b64encode(bytes(raw)).decode("ascii"),
        ),
    )
    forged.validate()

    with pytest.raises(ValueError, match="Ed25519 signature"):
        registry.register(
            manifest=source,
            binding=binding,
            signed_evidence=forged,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=2),
        )

    assert registry.event_count == 0
    assert registry.state_root_sha256 == "0" * 64


def test_registry_rejects_binding_profile_and_manifest_substitution() -> None:
    registry = QualificationEvidenceRegistry()
    source, binding, signed, keyring = evidence(
        subject_version="build-0003",
        suffix="substitution",
    )

    with pytest.raises(ValueError, match="signed binding id mismatch|signed profile digest mismatch"):
        registry.register(
            manifest=source,
            binding=replace(binding, profile_sha256="f" * 64),
            signed_evidence=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=2),
        )

    with pytest.raises(ValueError, match="binding manifest id mismatch|binding manifest digest mismatch"):
        registry.register(
            manifest=replace(source, organisation_id="OTHER"),
            binding=binding,
            signed_evidence=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=2),
        )


def test_registry_rejects_registration_before_manifest_issuance() -> None:
    registry = QualificationEvidenceRegistry()
    source, binding, signed, keyring = evidence(
        subject_version="build-0004",
        suffix="early",
    )

    with pytest.raises(ValueError, match="before manifest issuance"):
        registry.register(
            manifest=source,
            binding=binding,
            signed_evidence=signed,
            keyring=keyring,
            observed_at=NOW - timedelta(seconds=1),
        )


def test_state_merkle_proof_authenticates_current_active_record() -> None:
    registry = QualificationEvidenceRegistry()
    first = evidence(subject_version="build-0005", suffix="first")
    second = evidence(subject_version="build-0006", suffix="second")
    first_record = register_bundle(registry, first, seconds=2)
    register_bundle(registry, second, seconds=3)

    proof = registry.state_proof(evidence_id=first_record.evidence_id)

    assert proof.record.status is EvidenceLifecycleStatus.ACTIVE
    assert proof.state_root_sha256 == registry.state_root_sha256
    assert verify_state_proof(proof)


def test_state_root_and_proof_change_after_revocation() -> None:
    registry = QualificationEvidenceRegistry()
    bundle = evidence(subject_version="build-0007", suffix="revoke")
    record = register_bundle(registry, bundle, seconds=2)
    active_root = registry.state_root_sha256
    active_proof = registry.state_proof(evidence_id=record.evidence_id)
    assert verify_state_proof(active_proof)

    registry.revoke(
        evidence_id=record.evidence_id,
        reason="PROVIDER_EVIDENCE_RETRACTED",
        observed_at=NOW + timedelta(seconds=3),
    )

    revoked_root = registry.state_root_sha256
    revoked_proof = registry.state_proof(evidence_id=record.evidence_id)
    assert revoked_root != active_root
    assert revoked_proof.record.status is EvidenceLifecycleStatus.REVOKED
    assert verify_state_proof(revoked_proof)
    assert not verify_state_proof(replace(active_proof, state_root_sha256=revoked_root))


def test_supersession_requires_active_same_profile_family_replacement() -> None:
    registry = QualificationEvidenceRegistry()
    old = evidence(subject_version="build-0008", suffix="old")
    new = evidence(subject_version="build-0009", suffix="new")
    old_record = register_bundle(registry, old, seconds=2)
    new_record = register_bundle(registry, new, seconds=3)

    updated = registry.supersede(
        evidence_id=old_record.evidence_id,
        replacement_evidence_id=new_record.evidence_id,
        reason="NEW_BUILD_QUALIFIED",
        observed_at=NOW + timedelta(seconds=4),
    )

    assert updated.status is EvidenceLifecycleStatus.SUPERSEDED
    assert updated.replacement_evidence_id == new_record.evidence_id
    decision = registry.verify(
        evidence_id=old_record.evidence_id,
        observed_at=NOW + timedelta(seconds=5),
    )
    assert decision.status is EvidenceVerificationStatus.SUPERSEDED
    assert decision.reason == "NEW_BUILD_QUALIFIED"
    assert verify_state_proof(
        registry.state_proof(evidence_id=old_record.evidence_id)
    )


def test_cross_profile_supersession_is_rejected() -> None:
    registry = QualificationEvidenceRegistry()
    old = evidence(subject_version="build-0010", suffix="old-profile")
    other = evidence(
        subject_version="build-0011",
        suffix="other-profile",
        profile_id="ASTRA_OTHER_PROFILE",
    )
    old_record = register_bundle(registry, old, seconds=2)
    other_record = register_bundle(registry, other, seconds=3)

    with pytest.raises(ValueError, match="scope mismatch"):
        registry.supersede(
            evidence_id=old_record.evidence_id,
            replacement_evidence_id=other_record.evidence_id,
            reason="INVALID_CROSS_PROFILE",
            observed_at=NOW + timedelta(seconds=4),
        )


def test_revocation_is_terminal_and_unknown_evidence_is_explicit() -> None:
    registry = QualificationEvidenceRegistry()
    bundle = evidence(subject_version="build-0012", suffix="terminal")
    record = register_bundle(registry, bundle, seconds=2)

    registry.revoke(
        evidence_id=record.evidence_id,
        reason="QUALIFICATION_RETRACTED",
        observed_at=NOW + timedelta(seconds=3),
    )

    decision = registry.verify(
        evidence_id=record.evidence_id,
        observed_at=NOW + timedelta(seconds=4),
    )
    assert decision.status is EvidenceVerificationStatus.REVOKED
    assert decision.reason == "QUALIFICATION_RETRACTED"

    with pytest.raises(ValueError, match="not ACTIVE"):
        registry.revoke(
            evidence_id=record.evidence_id,
            reason="SECOND_REVOKE",
            observed_at=NOW + timedelta(seconds=5),
        )

    unknown = registry.verify(
        evidence_id="qevidence_unknown",
        observed_at=NOW + timedelta(seconds=5),
    )
    assert unknown.status is EvidenceVerificationStatus.UNKNOWN
    assert unknown.reason == "EVIDENCE_NOT_REGISTERED"
    assert unknown.state_root_sha256 == registry.state_root_sha256


def test_duplicate_registration_and_lifecycle_time_regression_are_rejected() -> None:
    registry = QualificationEvidenceRegistry()
    bundle = evidence(subject_version="build-0013", suffix="duplicate")
    record = register_bundle(registry, bundle, seconds=2)

    with pytest.raises(ValueError, match="already registered"):
        register_bundle(registry, bundle, seconds=3)

    with pytest.raises(ValueError, match="time regression"):
        registry.revoke(
            evidence_id=record.evidence_id,
            reason="BACKDATED_REVOKE",
            observed_at=NOW + timedelta(seconds=1),
        )


def test_state_proof_rejects_unknown_evidence_and_tampered_path() -> None:
    registry = QualificationEvidenceRegistry()
    first = evidence(subject_version="build-0014", suffix="path-one")
    second = evidence(subject_version="build-0015", suffix="path-two")
    first_record = register_bundle(registry, first, seconds=2)
    register_bundle(registry, second, seconds=3)

    with pytest.raises(ValueError, match="not registered"):
        registry.state_proof(evidence_id="qevidence_unknown")

    proof = registry.state_proof(evidence_id=first_record.evidence_id)
    assert proof.audit_path
    tampered = replace(
        proof,
        audit_path=("f" * 64, *proof.audit_path[1:]),
    )
    assert not verify_state_proof(tampered)
