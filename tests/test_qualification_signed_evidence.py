from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.signed_evidence import (
    sign_qualification_manifest,
    verify_qualification_evidence,
)
from app.runtime.signing_authority_v108 import (
    RootSignedKeyringSnapshotV108,
    SignatureReplayErrorV108,
    SignatureReplayLedgerV108,
    SignatureVerificationErrorV108,
    SigningBackendV108,
    SigningPurposeV108,
    verify_keyring_snapshot_v108,
)
from tests.helpers_v108 import NOW, LocalProviderV108, authority_fixture, descriptor


def manifest() -> QualificationManifest:
    value = QualificationManifest(
        job_id="qjob_1234567890abcdef12345678",
        job_result_sha256="1" * 64,
        request_sha256="2" * 64,
        organisation_id="ASTRA_INTERNAL",
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version="54b0ab70d294653db7b93ec155e0ccefc6d718fc",
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
        profile_version="1.0.0",
        environment="mainnet-public-readonly",
        corpus_ids=("BYBIT:BTCUSDT:fixture",),
        provider_replay_sha256="3" * 64,
        adapter_conformance_sha256="4" * 64,
        marketdata_integrity_sha256="5" * 64,
        continuity_checkpoint_id="checkpoint-1",
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


def qualification_authority():
    root, _, existing_descriptors, _, _ = authority_fixture()
    qualification = LocalProviderV108.create(
        "qualification-key",
        SigningBackendV108.HSM,
    )
    qualification_descriptor = descriptor(
        qualification,
        owner="qualification-owner",
        purpose=SigningPurposeV108.QUALIFICATION_EVIDENCE,
    )
    snapshot = RootSignedKeyringSnapshotV108.sign(
        generation=1,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=30),
        keys=(*existing_descriptors, qualification_descriptor),
        root_provider=root,
    )
    verified = verify_keyring_snapshot_v108(
        snapshot,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_generation=0,
        observed_at=NOW,
    )
    return qualification, qualification_descriptor, verified


def signed_fixture():
    qualification, qualification_descriptor, verified = qualification_authority()
    value = manifest()
    signed = sign_qualification_manifest(
        manifest=value,
        provider=qualification,
        descriptor=qualification_descriptor,
        keyring_generation=verified.generation,
        signature_id="qualification-signature-1",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        nonce="qualification-nonce-1",
    )
    return value, signed, verified


def test_dedicated_qualification_purpose_does_not_break_existing_keyrings() -> None:
    _, _, _, _, verified = authority_fixture()

    assert verified.generation == 1
    assert all(
        key.purpose is not SigningPurposeV108.QUALIFICATION_EVIDENCE
        for key in verified.keys.values()
    )


def test_qualification_manifest_signature_verifies_independently() -> None:
    value, signed, verified = signed_fixture()

    result = verify_qualification_evidence(
        manifest=value,
        signed_evidence=signed,
        keyring=verified,
        observed_at=NOW + timedelta(seconds=1),
    )

    assert result.evidence_id == signed.evidence_id
    assert result.manifest_id == value.manifest_id
    assert result.manifest_sha256 == value.manifest_sha256
    assert result.signer_key_id == "qualification-key"
    assert result.signer_owner_id == "qualification-owner"
    assert result.signer_key_generation == 1
    assert result.keyring_generation == 1
    assert signed.envelope.purpose is SigningPurposeV108.QUALIFICATION_EVIDENCE
    assert signed.envelope.domain == "astra.qualification.evidence.v1"


def test_manifest_tampering_fails_signature_binding() -> None:
    value, signed, verified = signed_fixture()
    tampered = replace(value, organisation_id="OTHER_ORGANISATION")
    tampered.validate()

    with pytest.raises(ValueError, match="manifest id mismatch|manifest digest mismatch"):
        verify_qualification_evidence(
            manifest=tampered,
            signed_evidence=signed,
            keyring=verified,
            observed_at=NOW + timedelta(seconds=1),
        )


def test_wrong_signing_purpose_cannot_sign_qualification_manifest() -> None:
    _, providers, descriptors, _, _ = authority_fixture()
    release = providers[0]
    release_descriptor = descriptors[0]

    with pytest.raises(Exception, match="purpose"):
        sign_qualification_manifest(
            manifest=manifest(),
            provider=release,
            descriptor=release_descriptor,
            keyring_generation=1,
            signature_id="wrong-purpose-signature",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=2),
            nonce="wrong-purpose-nonce",
        )


def test_unknown_qualification_key_fails_against_legacy_keyring() -> None:
    value, signed, _ = signed_fixture()
    _, _, _, _, legacy_verified = authority_fixture()

    with pytest.raises(SignatureVerificationErrorV108, match="unknown signing key"):
        verify_qualification_evidence(
            manifest=value,
            signed_evidence=signed,
            keyring=legacy_verified,
            observed_at=NOW + timedelta(seconds=1),
        )


def test_qualification_signature_replay_is_rejected() -> None:
    value, signed, verified = signed_fixture()
    ledger = SignatureReplayLedgerV108()

    first = verify_qualification_evidence(
        manifest=value,
        signed_evidence=signed,
        keyring=verified,
        observed_at=NOW + timedelta(seconds=1),
        replay_ledger=ledger,
    )
    assert first.evidence_id == signed.evidence_id
    assert ledger.size == 1

    with pytest.raises(SignatureReplayErrorV108, match="replay"):
        verify_qualification_evidence(
            manifest=value,
            signed_evidence=signed,
            keyring=verified,
            observed_at=NOW + timedelta(seconds=1),
            replay_ledger=ledger,
        )


def test_signed_evidence_rejects_payload_and_domain_substitution() -> None:
    _, signed, _ = signed_fixture()

    with pytest.raises(ValueError, match="payload mismatch"):
        replace(signed, manifest_sha256="f" * 64).validate()

    with pytest.raises(ValueError, match="domain mismatch"):
        replace(
            signed,
            envelope=replace(
                signed.envelope,
                domain="astra.other.evidence.v1",
            ),
        ).validate()
