from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.evidence_registry import InMemoryQualificationEvidenceRegistry
from app.qualification.registry_checkpoint import (
    build_registry_checkpoint,
    sign_registry_checkpoint,
    verify_registry_checkpoint,
)
from app.runtime.signing_authority_v108 import (
    RootSignedKeyringSnapshotV108,
    SignatureReplayErrorV108,
    SignatureReplayLedgerV108,
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


def signed_checkpoint():
    provider, qual_descriptor, keyring = qualification_authority()
    registry = InMemoryQualificationEvidenceRegistry()
    checkpoint = build_registry_checkpoint(
        registry=registry,
        issued_at=NOW,
    )
    signed = sign_registry_checkpoint(
        checkpoint=checkpoint,
        provider=provider,
        descriptor=qual_descriptor,
        keyring_generation=keyring.generation,
        signature_id="registry-checkpoint-signature-1",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        nonce="registry-checkpoint-nonce-1",
    )
    return checkpoint, signed, keyring


def test_empty_registry_checkpoint_is_genesis_and_verifiable() -> None:
    checkpoint, signed, keyring = signed_checkpoint()

    assert checkpoint.event_count == 0
    assert checkpoint.registry_head_sha256 == "0" * 64
    assert checkpoint.checkpoint_id.startswith("qcheckpoint_")

    verified = verify_registry_checkpoint(
        signed_checkpoint=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=1),
    )

    assert verified.checkpoint_id == checkpoint.checkpoint_id
    assert verified.checkpoint_sha256 == checkpoint.checkpoint_sha256
    assert verified.registry_head_sha256 == "0" * 64
    assert verified.signer_key_id == "qualification-key"
    assert verified.signer_owner_id == "qualification-owner"


def test_registry_checkpoint_is_deterministic_for_same_head_and_time() -> None:
    registry = InMemoryQualificationEvidenceRegistry()

    first = build_registry_checkpoint(registry=registry, issued_at=NOW)
    second = build_registry_checkpoint(registry=registry, issued_at=NOW)

    assert first == second
    assert first.checkpoint_sha256 == second.checkpoint_sha256


def test_registry_checkpoint_tampering_fails_signature_verification() -> None:
    checkpoint, signed, keyring = signed_checkpoint()
    tampered = replace(
        signed,
        checkpoint=replace(
            checkpoint,
            registry_head_sha256="f" * 64,
        ),
    )
    tampered.checkpoint.validate()

    with pytest.raises(ValueError, match="payload mismatch"):
        tampered.validate()

    with pytest.raises(ValueError, match="payload mismatch"):
        verify_registry_checkpoint(
            signed_checkpoint=tampered,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=1),
        )


def test_registry_checkpoint_signature_cannot_predate_checkpoint() -> None:
    provider, qual_descriptor, keyring = qualification_authority()
    registry = InMemoryQualificationEvidenceRegistry()
    checkpoint = build_registry_checkpoint(
        registry=registry,
        issued_at=NOW + timedelta(seconds=10),
    )

    with pytest.raises(ValueError, match="cannot predate"):
        sign_registry_checkpoint(
            checkpoint=checkpoint,
            provider=provider,
            descriptor=qual_descriptor,
            keyring_generation=keyring.generation,
            signature_id="registry-checkpoint-signature-predate",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=2),
            nonce="registry-checkpoint-nonce-predate",
        )


def test_registry_checkpoint_replay_is_rejected() -> None:
    _, signed, keyring = signed_checkpoint()
    ledger = SignatureReplayLedgerV108()

    verify_registry_checkpoint(
        signed_checkpoint=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=1),
        replay_ledger=ledger,
    )
    assert ledger.size == 1

    with pytest.raises(SignatureReplayErrorV108, match="replay"):
        verify_registry_checkpoint(
            signed_checkpoint=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=1),
            replay_ledger=ledger,
        )


def test_registry_checkpoint_requires_qualification_purpose() -> None:
    root, providers, descriptors, _, _ = authority_fixture()
    release = providers[0]
    release_descriptor = descriptors[0]
    checkpoint = build_registry_checkpoint(
        registry=InMemoryQualificationEvidenceRegistry(),
        issued_at=NOW,
    )

    with pytest.raises(Exception, match="purpose"):
        sign_registry_checkpoint(
            checkpoint=checkpoint,
            provider=release,
            descriptor=release_descriptor,
            keyring_generation=1,
            signature_id="wrong-purpose-checkpoint",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=2),
            nonce="wrong-purpose-checkpoint",
        )

    assert root is not None
