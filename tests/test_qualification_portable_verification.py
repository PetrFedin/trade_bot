from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.evidence_registry import (
    EvidenceVerificationStatus,
    InMemoryQualificationEvidenceRegistry,
)
from app.qualification.portable_verification import (
    PortableQualificationVerificationBundle,
    verify_portable_qualification_bundle,
)
from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.registry_checkpoint import (
    build_registry_checkpoint,
    sign_registry_checkpoint,
)
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


def manifest() -> QualificationManifest:
    value = QualificationManifest(
        job_id="qjob_1234567890abcdef12345678",
        job_result_sha256="1" * 64,
        request_sha256="2" * 64,
        organisation_id="ASTRA_INTERNAL",
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version="portable-build-1",
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
        profile_version="1.0.0",
        environment="mainnet-public-readonly",
        corpus_ids=("BYBIT:BTCUSDT:portable",),
        provider_replay_sha256="3" * 64,
        adapter_conformance_sha256="4" * 64,
        marketdata_integrity_sha256="5" * 64,
        continuity_checkpoint_id="checkpoint-portable",
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


def active_bundle():
    provider, qual_descriptor, keyring = qualification_authority()
    value = manifest()
    signed = sign_qualification_manifest(
        manifest=value,
        provider=provider,
        descriptor=qual_descriptor,
        keyring_generation=keyring.generation,
        signature_id="portable-evidence-signature",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        nonce="portable-evidence-nonce",
    )
    evidence_verification = verify_qualification_evidence(
        manifest=value,
        signed_evidence=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=1),
    )

    registry = InMemoryQualificationEvidenceRegistry()
    registry.register(
        manifest=value,
        signed_evidence=signed,
        verification=evidence_verification,
        observed_at=NOW + timedelta(seconds=2),
    )
    lifecycle = registry.verify(
        evidence_id=signed.evidence_id,
        observed_at=NOW + timedelta(seconds=3),
    )
    checkpoint = build_registry_checkpoint(
        registry=registry,
        issued_at=NOW + timedelta(seconds=4),
    )
    signed_checkpoint = sign_registry_checkpoint(
        checkpoint=checkpoint,
        provider=provider,
        descriptor=qual_descriptor,
        keyring_generation=keyring.generation,
        signature_id="portable-registry-signature",
        issued_at=NOW + timedelta(seconds=4),
        expires_at=NOW + timedelta(minutes=5),
        nonce="portable-registry-nonce",
    )
    bundle = PortableQualificationVerificationBundle(
        manifest=value,
        signed_evidence=signed,
        lifecycle=lifecycle,
        signed_registry_checkpoint=signed_checkpoint,
    )
    bundle.validate()
    return bundle, registry, provider, qual_descriptor, keyring


def test_portable_bundle_verifies_without_trusting_transport() -> None:
    bundle, _, _, _, keyring = active_bundle()

    result = verify_portable_qualification_bundle(
        bundle=bundle,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=5),
    )

    assert result.bundle_id == bundle.bundle_id
    assert result.bundle_sha256 == bundle.bundle_sha256
    assert result.lifecycle_status is EvidenceVerificationStatus.VALID
    assert result.evidence_id == bundle.signed_evidence.evidence_id
    assert result.manifest_sha256 == bundle.manifest.manifest_sha256
    assert result.registry_head_sha256 == (
        bundle.signed_registry_checkpoint.checkpoint.registry_head_sha256
    )
    assert result.evidence_signer_key_id == "qualification-key"
    assert result.registry_signer_key_id == "qualification-key"


def test_portable_bundle_is_deterministic() -> None:
    bundle, _, _, _, _ = active_bundle()

    assert bundle.bundle_sha256 == bundle.bundle_sha256
    assert bundle.bundle_id == f"qverify_{bundle.bundle_sha256[:24]}"
    assert bundle.payload() == bundle.payload()


def test_portable_bundle_rejects_manifest_substitution() -> None:
    bundle, _, _, _, keyring = active_bundle()
    tampered = replace(
        bundle,
        manifest=replace(bundle.manifest, organisation_id="OTHER_ORGANISATION"),
    )
    tampered.manifest.validate()

    with pytest.raises(ValueError, match="manifest id mismatch|manifest digest mismatch"):
        verify_portable_qualification_bundle(
            bundle=tampered,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=5),
        )


def test_portable_bundle_rejects_registry_head_substitution() -> None:
    bundle, _, _, _, keyring = active_bundle()
    lifecycle = replace(
        bundle.lifecycle,
        registry_head_sha256="f" * 64,
    )
    lifecycle.validate()
    tampered = replace(bundle, lifecycle=lifecycle)

    with pytest.raises(ValueError, match="registry head mismatch"):
        verify_portable_qualification_bundle(
            bundle=tampered,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=5),
        )


def test_portable_bundle_reports_revoked_evidence_with_signed_current_head() -> None:
    bundle, registry, provider, qual_descriptor, keyring = active_bundle()
    registry.revoke(
        evidence_id=bundle.signed_evidence.evidence_id,
        reason="QUALIFICATION_RETRACTED",
        observed_at=NOW + timedelta(seconds=6),
    )
    lifecycle = registry.verify(
        evidence_id=bundle.signed_evidence.evidence_id,
        observed_at=NOW + timedelta(seconds=7),
    )
    checkpoint = build_registry_checkpoint(
        registry=registry,
        issued_at=NOW + timedelta(seconds=8),
    )
    signed_checkpoint = sign_registry_checkpoint(
        checkpoint=checkpoint,
        provider=provider,
        descriptor=qual_descriptor,
        keyring_generation=keyring.generation,
        signature_id="portable-registry-revoked-signature",
        issued_at=NOW + timedelta(seconds=8),
        expires_at=NOW + timedelta(minutes=5),
        nonce="portable-registry-revoked-nonce",
    )
    revoked_bundle = replace(
        bundle,
        lifecycle=lifecycle,
        signed_registry_checkpoint=signed_checkpoint,
    )

    result = verify_portable_qualification_bundle(
        bundle=revoked_bundle,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=9),
    )

    assert result.lifecycle_status is EvidenceVerificationStatus.REVOKED
    assert result.replacement_evidence_id is None


def test_portable_bundle_rejects_unsigned_unknown_lifecycle() -> None:
    bundle, registry, _, _, keyring = active_bundle()
    unknown = registry.verify(
        evidence_id="qevidence_unknown",
        observed_at=NOW + timedelta(seconds=4),
    )
    tampered = replace(bundle, lifecycle=unknown)

    with pytest.raises(ValueError, match="evidence id mismatch|registered evidence"):
        verify_portable_qualification_bundle(
            bundle=tampered,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=5),
        )
