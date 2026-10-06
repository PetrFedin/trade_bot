from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.evidence_registry import (
    EvidenceLifecycleStatus,
    EvidenceVerificationStatus,
    InMemoryQualificationEvidenceRegistry,
)
from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.signed_evidence import (
    sign_qualification_manifest,
    verify_qualification_evidence,
)
from app.runtime.signing_authority_v108 import (
    RootSignedKeyringSnapshotV108,
    SigningBackendV108,
    SigningPurposeV108,
    verify_keyring_snapshot_v108,
)
from tests.helpers_v108 import NOW, LocalProviderV108, authority_fixture, descriptor


def manifest(*, subject_version: str, issued_offset: int = 0) -> QualificationManifest:
    value = QualificationManifest(
        job_id=f"qjob_{subject_version[:24]}",
        job_result_sha256="1" * 64,
        request_sha256="2" * 64,
        organisation_id="ASTRA_INTERNAL",
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version=subject_version,
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
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
        issued_at=NOW + timedelta(seconds=issued_offset),
    )
    value.validate()
    return value


def qualification_authority():
    root, _, existing_descriptors, _, _ = authority_fixture()
    provider = LocalProviderV108.create("qualification-key", SigningBackendV108.HSM)
    qual_descriptor = descriptor(
        provider,
        owner="qualification-owner",
        purpose=SigningPurposeV108.QUALIFICATION_EVIDENCE,
    )
    snapshot = RootSignedKeyringSnapshotV108.sign(
        generation=1,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=30),
        keys=(*existing_descriptors, qual_descriptor),
        root_provider=root,
    )
    verified = verify_keyring_snapshot_v108(
        snapshot,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_generation=0,
        observed_at=NOW,
    )
    return provider, qual_descriptor, verified


def evidence_bundle(
    *,
    subject_version: str,
    signature_suffix: str,
    issued_offset: int = 0,
):
    provider, qual_descriptor, keyring = qualification_authority()
    value = manifest(subject_version=subject_version, issued_offset=issued_offset)
    issued = NOW + timedelta(seconds=issued_offset)
    signed = sign_qualification_manifest(
        manifest=value,
        provider=provider,
        descriptor=qual_descriptor,
        keyring_generation=keyring.generation,
        signature_id=f"qualification-signature-{signature_suffix}",
        issued_at=issued,
        expires_at=issued + timedelta(minutes=2),
        nonce=f"qualification-nonce-{signature_suffix}",
    )
    verified = verify_qualification_evidence(
        manifest=value,
        signed_evidence=signed,
        keyring=keyring,
        observed_at=issued + timedelta(seconds=1),
    )
    return value, signed, verified


def test_registry_registers_verified_evidence_and_builds_hash_chain() -> None:
    registry = InMemoryQualificationEvidenceRegistry()
    value, signed, verified = evidence_bundle(
        subject_version="version-000000000000000000000001",
        signature_suffix="1",
    )

    record = registry.register(
        manifest=value,
        signed_evidence=signed,
        verification=verified,
        observed_at=NOW + timedelta(seconds=2),
    )

    assert record.status is EvidenceLifecycleStatus.ACTIVE
    assert record.evidence_id == signed.evidence_id
    assert registry.event_count == 1
    assert registry.head_sha256 == record.latest_event_sha256
    assert registry.verify_chain() == registry.head_sha256

    decision = registry.verify(
        evidence_id=signed.evidence_id,
        observed_at=NOW + timedelta(seconds=3),
    )
    assert decision.status is EvidenceVerificationStatus.VALID
    assert decision.reason is None
    assert decision.manifest_sha256 == value.manifest_sha256


def test_registry_supersedes_only_with_registered_active_matching_scope() -> None:
    registry = InMemoryQualificationEvidenceRegistry()
    old_manifest, old_signed, old_verified = evidence_bundle(
        subject_version="version-000000000000000000000001",
        signature_suffix="old",
    )
    new_manifest, new_signed, new_verified = evidence_bundle(
        subject_version="version-000000000000000000000002",
        signature_suffix="new",
        issued_offset=10,
    )
    registry.register(
        manifest=old_manifest,
        signed_evidence=old_signed,
        verification=old_verified,
        observed_at=NOW + timedelta(seconds=2),
    )
    registry.register(
        manifest=new_manifest,
        signed_evidence=new_signed,
        verification=new_verified,
        observed_at=NOW + timedelta(seconds=12),
    )

    updated = registry.supersede(
        evidence_id=old_signed.evidence_id,
        replacement_evidence_id=new_signed.evidence_id,
        reason="NEW_SUBJECT_VERSION_QUALIFIED",
        observed_at=NOW + timedelta(seconds=13),
    )

    assert updated.status is EvidenceLifecycleStatus.SUPERSEDED
    assert updated.replacement_evidence_id == new_signed.evidence_id
    decision = registry.verify(
        evidence_id=old_signed.evidence_id,
        observed_at=NOW + timedelta(seconds=14),
    )
    assert decision.status is EvidenceVerificationStatus.SUPERSEDED
    assert decision.replacement_evidence_id == new_signed.evidence_id
    assert decision.reason == "NEW_SUBJECT_VERSION_QUALIFIED"
    assert registry.verify_chain() == registry.head_sha256


def test_registry_revocation_is_terminal_and_visible_to_verifier() -> None:
    registry = InMemoryQualificationEvidenceRegistry()
    value, signed, verified = evidence_bundle(
        subject_version="version-000000000000000000000003",
        signature_suffix="revoke",
    )
    registry.register(
        manifest=value,
        signed_evidence=signed,
        verification=verified,
        observed_at=NOW + timedelta(seconds=2),
    )

    revoked = registry.revoke(
        evidence_id=signed.evidence_id,
        reason="PROVIDER_EVIDENCE_RETRACTED",
        observed_at=NOW + timedelta(seconds=3),
    )
    assert revoked.status is EvidenceLifecycleStatus.REVOKED

    decision = registry.verify(
        evidence_id=signed.evidence_id,
        observed_at=NOW + timedelta(seconds=4),
    )
    assert decision.status is EvidenceVerificationStatus.REVOKED
    assert decision.reason == "PROVIDER_EVIDENCE_RETRACTED"

    with pytest.raises(ValueError, match="not ACTIVE"):
        registry.revoke(
            evidence_id=signed.evidence_id,
            reason="SECOND_REVOKE",
            observed_at=NOW + timedelta(seconds=5),
        )


def test_registry_unknown_evidence_is_fail_closed() -> None:
    registry = InMemoryQualificationEvidenceRegistry()

    decision = registry.verify(
        evidence_id="qevidence_unknown",
        observed_at=NOW,
    )

    assert decision.status is EvidenceVerificationStatus.UNKNOWN
    assert decision.reason == "EVIDENCE_NOT_REGISTERED"
    assert decision.registry_head_sha256 == "0" * 64


def test_registry_rejects_unbound_verification_record() -> None:
    registry = InMemoryQualificationEvidenceRegistry()
    value, signed, verified = evidence_bundle(
        subject_version="version-000000000000000000000004",
        signature_suffix="mismatch",
    )

    with pytest.raises(ValueError, match="verification evidence mismatch"):
        registry.register(
            manifest=value,
            signed_evidence=signed,
            verification=replace(verified, evidence_id="qevidence_other"),
            observed_at=NOW + timedelta(seconds=2),
        )


def test_registry_rejects_duplicate_registration_and_time_regression() -> None:
    registry = InMemoryQualificationEvidenceRegistry()
    value, signed, verified = evidence_bundle(
        subject_version="version-000000000000000000000005",
        signature_suffix="dup",
    )
    registry.register(
        manifest=value,
        signed_evidence=signed,
        verification=verified,
        observed_at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            manifest=value,
            signed_evidence=signed,
            verification=verified,
            observed_at=NOW + timedelta(seconds=3),
        )

    with pytest.raises(ValueError, match="time regression"):
        registry.revoke(
            evidence_id=signed.evidence_id,
            reason="INVALID_TIME",
            observed_at=NOW + timedelta(seconds=1),
        )


def test_registry_rejects_cross_profile_supersession() -> None:
    registry = InMemoryQualificationEvidenceRegistry()
    old_manifest, old_signed, old_verified = evidence_bundle(
        subject_version="version-000000000000000000000006",
        signature_suffix="old-profile",
    )
    new_manifest, new_signed, new_verified = evidence_bundle(
        subject_version="version-000000000000000000000007",
        signature_suffix="new-profile",
        issued_offset=10,
    )
    new_manifest = replace(new_manifest, profile_id="OTHER_PROFILE")
    new_manifest.validate()
    provider, qual_descriptor, keyring = qualification_authority()
    issued = NOW + timedelta(seconds=10)
    new_signed = sign_qualification_manifest(
        manifest=new_manifest,
        provider=provider,
        descriptor=qual_descriptor,
        keyring_generation=keyring.generation,
        signature_id="qualification-signature-cross-profile",
        issued_at=issued,
        expires_at=issued + timedelta(minutes=2),
        nonce="qualification-nonce-cross-profile",
    )
    new_verified = verify_qualification_evidence(
        manifest=new_manifest,
        signed_evidence=new_signed,
        keyring=keyring,
        observed_at=issued + timedelta(seconds=1),
    )

    registry.register(
        manifest=old_manifest,
        signed_evidence=old_signed,
        verification=old_verified,
        observed_at=NOW + timedelta(seconds=2),
    )
    registry.register(
        manifest=new_manifest,
        signed_evidence=new_signed,
        verification=new_verified,
        observed_at=NOW + timedelta(seconds=12),
    )

    with pytest.raises(ValueError, match="scope mismatch"):
        registry.supersede(
            evidence_id=old_signed.evidence_id,
            replacement_evidence_id=new_signed.evidence_id,
            reason="INVALID_CROSS_PROFILE",
            observed_at=NOW + timedelta(seconds=13),
        )
