from __future__ import annotations

from base64 import b64encode
from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.signed_evidence import (
    sign_qualification_manifest,
    verify_qualification_evidence,
)
from app.qualification.signing_authority import (
    QualificationKeyringSnapshot,
    QualificationSignatureReplayLedger,
    QualificationSigningKeyDescriptor,
    verify_qualification_keyring,
)
from app.runtime.signing_authority_v108 import SigningBackendV108
from tests.helpers_v108 import NOW, LocalProviderV108


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


def qualification_descriptor(
    provider: LocalProviderV108,
    *,
    owner_id: str = "qualification-owner",
    revoked_at=None,
) -> QualificationSigningKeyDescriptor:
    value = QualificationSigningKeyDescriptor(
        key_id=provider.key_id,
        owner_id=owner_id,
        backend=getattr(provider.backend, "value", provider.backend),
        generation=provider.generation,
        public_key_b64=b64encode(provider.public_key_bytes()).decode("ascii"),
        not_before=NOW - timedelta(hours=1),
        not_after=NOW + timedelta(hours=1),
        revoked_at=revoked_at,
    )
    value.validate()
    return value


def qualification_authority():
    root = LocalProviderV108.create("qualification-root", SigningBackendV108.HSM)
    qualification = LocalProviderV108.create(
        "qualification-key",
        SigningBackendV108.HSM,
    )
    descriptor = qualification_descriptor(qualification)
    snapshot = QualificationKeyringSnapshot.sign(
        generation=1,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=30),
        keys=(descriptor,),
        root_provider=root,
    )
    verified = verify_qualification_keyring(
        snapshot,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_generation=0,
        observed_at=NOW,
    )
    return root, qualification, descriptor, snapshot, verified


def signed_fixture():
    _, qualification, descriptor, _, verified = qualification_authority()
    value = manifest()
    signed = sign_qualification_manifest(
        manifest=value,
        provider=qualification,
        descriptor=descriptor,
        keyring_generation=verified.generation,
        signature_id="qualification-signature-1",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        nonce="qualification-nonce-1",
    )
    return value, signed, verified


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


def test_wrong_descriptor_cannot_sign_qualification_manifest() -> None:
    _, qualification, _, _, verified = qualification_authority()
    other = LocalProviderV108.create("other-key", SigningBackendV108.HSM)
    other_descriptor = qualification_descriptor(other)

    with pytest.raises(ValueError, match="key mismatch"):
        sign_qualification_manifest(
            manifest=manifest(),
            provider=qualification,
            descriptor=other_descriptor,
            keyring_generation=verified.generation,
            signature_id="wrong-key-signature",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=2),
            nonce="wrong-key-nonce",
        )


def test_unknown_qualification_key_fails_against_other_keyring() -> None:
    value, signed, _ = signed_fixture()
    root = LocalProviderV108.create("other-root", SigningBackendV108.HSM)
    other = LocalProviderV108.create("other-key", SigningBackendV108.KMS)
    other_descriptor = qualification_descriptor(other, owner_id="other-owner")
    snapshot = QualificationKeyringSnapshot.sign(
        generation=1,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=30),
        keys=(other_descriptor,),
        root_provider=root,
    )
    verified = verify_qualification_keyring(
        snapshot,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_generation=0,
        observed_at=NOW,
    )

    with pytest.raises(ValueError, match="unknown qualification signing key"):
        verify_qualification_evidence(
            manifest=value,
            signed_evidence=signed,
            keyring=verified,
            observed_at=NOW + timedelta(seconds=1),
        )


def test_qualification_signature_replay_is_rejected() -> None:
    value, signed, verified = signed_fixture()
    ledger = QualificationSignatureReplayLedger()

    first = verify_qualification_evidence(
        manifest=value,
        signed_evidence=signed,
        keyring=verified,
        observed_at=NOW + timedelta(seconds=1),
        replay_ledger=ledger,
    )
    assert first.evidence_id == signed.evidence_id
    assert ledger.size == 1

    with pytest.raises(ValueError, match="replay"):
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


def test_qualification_keyring_is_root_signed_and_generation_monotonic() -> None:
    root, _, descriptor, snapshot, _ = qualification_authority()

    with pytest.raises(ValueError, match="not monotonic"):
        verify_qualification_keyring(
            snapshot,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_generation=1,
            observed_at=NOW,
        )

    tampered = replace(
        snapshot,
        keys=(replace(descriptor, owner_id="tampered-owner"),),
    )
    with pytest.raises(ValueError, match="root signature"):
        verify_qualification_keyring(
            tampered,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_generation=0,
            observed_at=NOW,
        )


def test_revoked_qualification_key_is_fail_closed() -> None:
    root = LocalProviderV108.create("qualification-root", SigningBackendV108.HSM)
    provider = LocalProviderV108.create("qualification-key", SigningBackendV108.HSM)
    descriptor = qualification_descriptor(
        provider,
        revoked_at=NOW - timedelta(seconds=1),
    )
    snapshot = QualificationKeyringSnapshot.sign(
        generation=1,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=30),
        keys=(descriptor,),
        root_provider=root,
    )
    verified = verify_qualification_keyring(
        snapshot,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_generation=0,
        observed_at=NOW,
    )

    with pytest.raises(ValueError, match="inactive or revoked"):
        verified.require_key(
            key_id=descriptor.key_id,
            key_generation=descriptor.generation,
            observed_at=NOW,
        )
