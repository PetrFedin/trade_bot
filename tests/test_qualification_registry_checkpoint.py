from __future__ import annotations

from base64 import b64encode
from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.evidence_registry import QualificationEvidenceRegistry
from app.qualification.registry_checkpoint import (
    build_registry_checkpoint,
    sign_registry_checkpoint,
    verify_registry_checkpoint,
)
from app.qualification.signing_authority import (
    QualificationKeyringSnapshot,
    QualificationSignatureReplayLedger,
    QualificationSigningKeyDescriptor,
    verify_qualification_keyring,
)
from app.runtime.signing_authority_v108 import SigningBackendV108
from tests.helpers_v108 import NOW, LocalProviderV108


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


def signed_checkpoint():
    signer, descriptor, keyring = authority()
    checkpoint = build_registry_checkpoint(
        registry=QualificationEvidenceRegistry(),
        issued_at=NOW,
    )
    signed = sign_registry_checkpoint(
        checkpoint=checkpoint,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="registry-checkpoint-1",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        nonce="registry-checkpoint-nonce-1",
    )
    return checkpoint, signed, keyring


def test_empty_registry_checkpoint_is_genesis_and_verifiable() -> None:
    checkpoint, signed, keyring = signed_checkpoint()

    assert checkpoint.event_count == 0
    assert checkpoint.registry_head_sha256 == "0" * 64

    verified = verify_registry_checkpoint(
        signed_checkpoint=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=1),
    )

    assert verified.checkpoint_id == checkpoint.checkpoint_id
    assert verified.registry_head_sha256 == "0" * 64
    assert verified.signer_key_id == "qualification-key"
    assert verified.signer_owner_id == "qualification-owner"


def test_checkpoint_tampering_breaks_payload_binding() -> None:
    checkpoint, signed, keyring = signed_checkpoint()
    tampered = replace(
        signed,
        checkpoint=replace(checkpoint, registry_head_sha256="f" * 64),
    )

    with pytest.raises(ValueError, match="payload mismatch"):
        verify_registry_checkpoint(
            signed_checkpoint=tampered,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=1),
        )


def test_checkpoint_signature_cannot_predate_checkpoint() -> None:
    signer, descriptor, keyring = authority()
    checkpoint = build_registry_checkpoint(
        registry=QualificationEvidenceRegistry(),
        issued_at=NOW + timedelta(seconds=10),
    )

    with pytest.raises(ValueError, match="cannot predate"):
        sign_registry_checkpoint(
            checkpoint=checkpoint,
            provider=signer,
            descriptor=descriptor,
            keyring_generation=keyring.generation,
            signature_id="registry-checkpoint-predate",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=2),
            nonce="registry-checkpoint-predate-nonce",
        )


def test_checkpoint_replay_is_rejected() -> None:
    _, signed, keyring = signed_checkpoint()
    ledger = QualificationSignatureReplayLedger()

    verify_registry_checkpoint(
        signed_checkpoint=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=1),
        replay_ledger=ledger,
    )
    assert ledger.size == 1

    with pytest.raises(ValueError, match="replay"):
        verify_registry_checkpoint(
            signed_checkpoint=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=1),
            replay_ledger=ledger,
        )


def test_nonempty_checkpoint_cannot_claim_genesis_root() -> None:
    checkpoint, _, _ = signed_checkpoint()

    with pytest.raises(ValueError, match="non-empty"):
        replace(
            checkpoint,
            event_count=1,
            registry_head_sha256="0" * 64,
        ).validate()
