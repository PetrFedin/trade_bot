from __future__ import annotations

from base64 import b64encode
from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.evidence_registry import (
    EvidenceLifecycleStatus,
    EvidenceVerificationStatus,
    QualificationEvidenceRegistry,
)
from app.qualification.profile_binding import ProfileBoundQualificationManifest
from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.signed_evidence import (
    sign_qualification_binding,
    verify_qualification_evidence,
)
from app.qualification.signing_authority import (
    QualificationKeyringSnapshot,
    QualificationSigningKeyDescriptor,
    verify_qualification_keyring,
)
from app.runtime.signing_authority_v108 import SigningBackendV108
from tests.helpers_v108 import NOW, LocalProviderV108


def manifest(*, subject_version: str) -> QualificationManifest:
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


def evidence(*, subject_version: str, suffix: str):
    signer, descriptor, keyring = authority()
    source = manifest(subject_version=subject_version)
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
    verified = verify_qualification_evidence(
        binding=binding,
        signed_evidence=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=1),
    )
    return source, binding, signed, verified


def test_registry_accepts_only_fully_bound_verified_evidence() -> None:
    registry = QualificationEvidenceRegistry()
    source, binding, signed, verified = evidence(
        subject_version="build-0001",
        suffix="one",
    )

    record = registry.register(
        manifest=source,
        binding=binding,
        signed_evidence=signed,
        verification=verified,
        observed_at=NOW + timedelta(seconds=2),
    )

    assert record.status is EvidenceLifecycleStatus.ACTIVE
    assert record.binding_sha256 == binding.binding_sha256
    assert record.manifest_sha256 == source.manifest_sha256
    assert record.profile_sha256 == binding.profile_sha256
    assert registry.event_count == 1
    assert registry.verify_chain() == registry.head_sha256

    decision = registry.verify(
        evidence_id=signed.evidence_id,
        observed_at=NOW + timedelta(seconds=3),
    )
    assert decision.status is EvidenceVerificationStatus.VALID
    assert decision.reason is None
    assert decision.binding_sha256 == binding.binding_sha256


def test_registry_rejects_profile_or_binding_substitution() -> None:
    registry = QualificationEvidenceRegistry()
    source, binding, signed, verified = evidence(
        subject_version="build-0002",
        suffix="two",
    )

    with pytest.raises(ValueError, match="signed profile digest mismatch"):
        registry.register(
            manifest=source,
            binding=replace(binding, profile_sha256="f" * 64),
            signed_evidence=signed,
            verification=verified,
            observed_at=NOW + timedelta(seconds=2),
        )


def test_registry_supersession_requires_active_matching_scope_replacement() -> None:
    registry = QualificationEvidenceRegistry()
    old = evidence(subject_version="build-0003", suffix="old")
    new = evidence(subject_version="build-0004", suffix="new")
    old_record = registry.register(
        manifest=old[0],
        binding=old[1],
        signed_evidence=old[2],
        verification=old[3],
        observed_at=NOW + timedelta(seconds=2),
    )
    new_record = registry.register(
        manifest=new[0],
        binding=new[1],
        signed_evidence=new[2],
        verification=new[3],
        observed_at=NOW + timedelta(seconds=3),
    )

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


def test_registry_revocation_is_terminal_and_fail_closed() -> None:
    registry = QualificationEvidenceRegistry()
    source, binding, signed, verified = evidence(
        subject_version="build-0005",
        suffix="revoke",
    )
    record = registry.register(
        manifest=source,
        binding=binding,
        signed_evidence=signed,
        verification=verified,
        observed_at=NOW + timedelta(seconds=2),
    )

    revoked = registry.revoke(
        evidence_id=record.evidence_id,
        reason="PROVIDER_EVIDENCE_RETRACTED",
        observed_at=NOW + timedelta(seconds=3),
    )
    assert revoked.status is EvidenceLifecycleStatus.REVOKED

    decision = registry.verify(
        evidence_id=record.evidence_id,
        observed_at=NOW + timedelta(seconds=4),
    )
    assert decision.status is EvidenceVerificationStatus.REVOKED
    assert decision.reason == "PROVIDER_EVIDENCE_RETRACTED"

    with pytest.raises(ValueError, match="not ACTIVE"):
        registry.revoke(
            evidence_id=record.evidence_id,
            reason="SECOND_REVOKE",
            observed_at=NOW + timedelta(seconds=5),
        )


def test_unknown_evidence_is_explicitly_unknown() -> None:
    registry = QualificationEvidenceRegistry()

    decision = registry.verify(
        evidence_id="qevidence_unknown",
        observed_at=NOW,
    )

    assert decision.status is EvidenceVerificationStatus.UNKNOWN
    assert decision.reason == "EVIDENCE_NOT_REGISTERED"
    assert decision.registry_head_sha256 == "0" * 64


def test_registry_rejects_duplicate_registration_and_time_regression() -> None:
    registry = QualificationEvidenceRegistry()
    source, binding, signed, verified = evidence(
        subject_version="build-0006",
        suffix="duplicate",
    )
    record = registry.register(
        manifest=source,
        binding=binding,
        signed_evidence=signed,
        verification=verified,
        observed_at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            manifest=source,
            binding=binding,
            signed_evidence=signed,
            verification=verified,
            observed_at=NOW + timedelta(seconds=3),
        )

    with pytest.raises(ValueError, match="time regression"):
        registry.revoke(
            evidence_id=record.evidence_id,
            reason="BACKDATED_REVOKE",
            observed_at=NOW + timedelta(seconds=1),
        )
