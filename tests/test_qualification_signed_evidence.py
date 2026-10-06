from __future__ import annotations

from base64 import b64encode
from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.profile_binding import ProfileBoundQualificationManifest
from app.qualification.signed_evidence import (
    sign_qualification_binding,
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


def binding() -> ProfileBoundQualificationManifest:
    value = ProfileBoundQualificationManifest(
        manifest_id="qmanifest_1234567890abcdef12345678",
        manifest_sha256="1" * 64,
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
        profile_version="1.0.0",
        profile_sha256="2" * 64,
        scope="PUBLIC_MARKET_DATA_ONLY",
        environment="mainnet-public-readonly",
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
    signer = LocalProviderV108.create("qualification-key", SigningBackendV108.HSM)
    descriptor = qualification_descriptor(signer)
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
    return root, signer, descriptor, snapshot, verified


def signed_fixture():
    _, signer, descriptor, _, keyring = qualification_authority()
    value = binding()
    signed = sign_qualification_binding(
        binding=value,
        provider=signer,
        descriptor=descriptor,
        keyring_generation=keyring.generation,
        signature_id="qualification-signature-1",
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        nonce="qualification-nonce-1",
    )
    return value, signed, keyring


def test_profile_bound_signature_verifies_independently() -> None:
    value, signed, keyring = signed_fixture()

    verified = verify_qualification_evidence(
        binding=value,
        signed_evidence=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=1),
    )

    assert verified.evidence_id == signed.evidence_id
    assert verified.binding_id == value.binding_id
    assert verified.binding_sha256 == value.binding_sha256
    assert verified.manifest_sha256 == value.manifest_sha256
    assert verified.profile_sha256 == value.profile_sha256
    assert verified.signer_key_id == "qualification-key"
    assert verified.signer_owner_id == "qualification-owner"
    assert signed.envelope.domain == "astra.qualification.profile-bound-evidence.v1"


def test_profile_substitution_breaks_signed_binding() -> None:
    value, signed, keyring = signed_fixture()
    substituted = replace(value, profile_sha256="f" * 64)
    substituted.validate()
    assert substituted.binding_sha256 != value.binding_sha256

    with pytest.raises(ValueError, match="binding id mismatch|binding digest mismatch"):
        verify_qualification_evidence(
            binding=substituted,
            signed_evidence=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=1),
        )


def test_manifest_substitution_breaks_signed_binding() -> None:
    value, signed, keyring = signed_fixture()
    substituted = replace(value, manifest_sha256="e" * 64)
    substituted.validate()

    with pytest.raises(ValueError, match="binding id mismatch|binding digest mismatch"):
        verify_qualification_evidence(
            binding=substituted,
            signed_evidence=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=1),
        )


def test_wrong_provider_key_cannot_sign_binding() -> None:
    _, signer, _, _, keyring = qualification_authority()
    other = LocalProviderV108.create("other-key", SigningBackendV108.HSM)
    other_descriptor = qualification_descriptor(other)

    with pytest.raises(ValueError, match="key mismatch"):
        sign_qualification_binding(
            binding=binding(),
            provider=signer,
            descriptor=other_descriptor,
            keyring_generation=keyring.generation,
            signature_id="wrong-key-signature",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=2),
            nonce="wrong-key-nonce",
        )


def test_unknown_qualification_key_is_fail_closed() -> None:
    value, signed, _ = signed_fixture()
    root = LocalProviderV108.create("other-root", SigningBackendV108.HSM)
    other = LocalProviderV108.create("other-key", SigningBackendV108.KMS)
    descriptor = qualification_descriptor(other, owner_id="other-owner")
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

    with pytest.raises(ValueError, match="unknown qualification signing key"):
        verify_qualification_evidence(
            binding=value,
            signed_evidence=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=1),
        )


def test_signature_replay_is_rejected() -> None:
    value, signed, keyring = signed_fixture()
    ledger = QualificationSignatureReplayLedger()

    verify_qualification_evidence(
        binding=value,
        signed_evidence=signed,
        keyring=keyring,
        observed_at=NOW + timedelta(seconds=1),
        replay_ledger=ledger,
    )
    assert ledger.size == 1

    with pytest.raises(ValueError, match="replay"):
        verify_qualification_evidence(
            binding=value,
            signed_evidence=signed,
            keyring=keyring,
            observed_at=NOW + timedelta(seconds=1),
            replay_ledger=ledger,
        )


def test_signed_evidence_rejects_payload_and_domain_substitution() -> None:
    _, signed, _ = signed_fixture()

    with pytest.raises(ValueError, match="payload mismatch"):
        replace(signed, binding_sha256="f" * 64).validate()

    with pytest.raises(ValueError, match="domain mismatch"):
        replace(
            signed,
            envelope=replace(
                signed.envelope,
                domain="astra.other.evidence.v1",
            ),
        ).validate()


def test_root_signed_keyring_rejects_generation_and_tampering() -> None:
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


def test_revoked_signing_key_is_fail_closed() -> None:
    root = LocalProviderV108.create("qualification-root", SigningBackendV108.HSM)
    signer = LocalProviderV108.create("qualification-key", SigningBackendV108.HSM)
    descriptor = qualification_descriptor(
        signer,
        revoked_at=NOW - timedelta(seconds=1),
    )
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

    with pytest.raises(ValueError, match="inactive or revoked"):
        keyring.require_key(
            key_id=descriptor.key_id,
            key_generation=descriptor.generation,
            observed_at=NOW,
        )
