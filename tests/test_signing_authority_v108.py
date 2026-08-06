from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import base64
import json

import pytest

from app.runtime.signing_authority_v108 import (
    KeyringVerificationErrorV108,
    RootSignedKeyringSnapshotV108,
    SignatureReplayErrorV108,
    SignatureReplayLedgerV108,
    SignatureVerificationErrorV108,
    SigningBackendV108,
    SigningPurposeV108,
    canonical_bytes_v108,
    sign_envelope_v108,
    verify_envelope_v108,
    verify_keyring_snapshot_v108,
    verify_rollout_authorization_v108,
)
from tests.helpers_v108 import NOW, LocalProviderV108, authority_fixture, authorization_bundle, descriptor


def test_root_signed_keyring_and_three_party_authorization() -> None:
    _, _, _, _, verified, bundle = authorization_bundle()
    keys = verify_rollout_authorization_v108(bundle, keyring=verified, observed_at=NOW)
    assert [key.purpose for key in keys] == [
        SigningPurposeV108.RELEASE_APPROVAL,
        SigningPurposeV108.RISK_APPROVAL,
        SigningPurposeV108.CONTROLLER_COMMAND,
    ]
    assert len({key.owner_id for key in keys}) == 3


def test_keyring_generation_must_be_monotonic() -> None:
    root, _, _, snapshot, _ = authority_fixture()
    with pytest.raises(KeyringVerificationErrorV108, match="not monotonic"):
        verify_keyring_snapshot_v108(
            snapshot,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_generation=1,
            observed_at=NOW,
        )


def test_root_signature_tampering_fails_closed() -> None:
    root, _, _, snapshot, _ = authority_fixture()
    raw = bytearray(base64.b64decode(snapshot.root_signature_b64))
    raw[0] ^= 1
    tampered = replace(snapshot, root_signature_b64=base64.b64encode(raw).decode("ascii"))
    with pytest.raises(KeyringVerificationErrorV108, match="root signature"):
        verify_keyring_snapshot_v108(
            tampered,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_generation=0,
            observed_at=NOW,
        )


def test_revoked_key_cannot_verify() -> None:
    root = LocalProviderV108.create("root-revoked", SigningBackendV108.HSM)
    release = LocalProviderV108.create("release-revoked", SigningBackendV108.KMS)
    others = [
        LocalProviderV108.create("risk-live", SigningBackendV108.KMS),
        LocalProviderV108.create("controller-live", SigningBackendV108.HSM),
        LocalProviderV108.create("executor-live", SigningBackendV108.HSM),
    ]
    descriptors = (
        descriptor(
            release,
            owner="release-owner",
            purpose=SigningPurposeV108.RELEASE_APPROVAL,
            revoked_at=NOW - timedelta(seconds=1),
        ),
        descriptor(others[0], owner="risk-owner", purpose=SigningPurposeV108.RISK_APPROVAL),
        descriptor(others[1], owner="controller-owner", purpose=SigningPurposeV108.CONTROLLER_COMMAND),
        descriptor(others[2], owner="executor-owner", purpose=SigningPurposeV108.EXECUTOR_RECEIPT),
    )
    snapshot = RootSignedKeyringSnapshotV108.sign(
        generation=1,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
        keys=descriptors,
        root_provider=root,
    )
    with pytest.raises(KeyringVerificationErrorV108, match="lacks active purposes"):
        verify_keyring_snapshot_v108(
            snapshot,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_generation=0,
            observed_at=NOW,
        )


def test_signature_domain_and_payload_are_bound() -> None:
    _, _, _, _, verified, bundle = authorization_bundle()
    with pytest.raises(SignatureVerificationErrorV108, match="domain"):
        verify_envelope_v108(
            bundle.release,
            keyring=verified,
            expected_purpose=SigningPurposeV108.RELEASE_APPROVAL,
            expected_domain="astra.other.domain.v108",
            expected_payload_digest=bundle.authorization_digest,
            observed_at=NOW,
        )
    with pytest.raises(SignatureVerificationErrorV108, match="payload"):
        verify_envelope_v108(
            bundle.release,
            keyring=verified,
            expected_purpose=SigningPurposeV108.RELEASE_APPROVAL,
            expected_domain="astra.rollout.authorization.v108",
            expected_payload_digest="f" * 64,
            observed_at=NOW,
        )


def test_replay_consumption_is_atomic_for_the_bundle() -> None:
    _, _, _, _, verified, bundle = authorization_bundle()
    ledger = SignatureReplayLedgerV108()
    verify_rollout_authorization_v108(bundle, keyring=verified, observed_at=NOW, replay_ledger=ledger)
    assert ledger.size == 3
    with pytest.raises(SignatureReplayErrorV108, match="replay"):
        verify_rollout_authorization_v108(bundle, keyring=verified, observed_at=NOW, replay_ledger=ledger)
    assert ledger.size == 3


def test_authorization_requires_distinct_owners() -> None:
    _, _, _, _, verified, bundle = authorization_bundle(risk_owner="release-owner")
    with pytest.raises(SignatureVerificationErrorV108, match="distinct owners"):
        verify_rollout_authorization_v108(bundle, keyring=verified, observed_at=NOW)


def test_provider_mismatch_and_invalid_provider_signature_fail() -> None:
    _, providers, descriptors, _, _ = authority_fixture()
    release, risk, _, _ = providers
    release_d, _, _, _ = descriptors
    with pytest.raises(Exception, match="provider does not match"):
        sign_envelope_v108(
            provider=risk,
            descriptor=release_d,
            keyring_generation=1,
            signature_id="sig-x",
            purpose=SigningPurposeV108.RELEASE_APPROVAL,
            domain="astra.rollout.authorization.v108",
            payload_digest="1" * 64,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
            nonce="nonce-x",
        )

    class BrokenProvider:
        key_id = release.key_id
        backend = release.backend
        generation = release.generation

        def public_key_bytes(self) -> bytes:
            return release.public_key_bytes()

        def sign(self, payload: bytes) -> bytes:
            return b"x" * 64

    with pytest.raises(SignatureVerificationErrorV108, match="invalid signature"):
        sign_envelope_v108(
            provider=BrokenProvider(),
            descriptor=release_d,
            keyring_generation=1,
            signature_id="sig-broken",
            purpose=SigningPurposeV108.RELEASE_APPROVAL,
            domain="astra.rollout.authorization.v108",
            payload_digest="1" * 64,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
            nonce="nonce-broken",
        )


def test_serialized_authority_objects_contain_no_private_key_material() -> None:
    _, _, _, snapshot, _, bundle = authorization_bundle()
    document = json.dumps(
        {"snapshot": snapshot.to_payload(), "bundle": bundle.to_payload()},
        default=str,
        sort_keys=True,
    )
    assert "_private" not in document
    assert "private_key" not in document
    assert len(canonical_bytes_v108(bundle.to_payload())) > 100


def test_executor_receipt_signature_is_bound_and_replay_protected() -> None:
    from app.runtime.signing_authority_v108 import verify_receipt_authorization_v108
    from tests.helpers_v108 import receipt_authorization

    _, providers, descriptors, _, verified, bundle = authorization_bundle()
    receipt = receipt_authorization(bundle, providers, descriptors)
    ledger = SignatureReplayLedgerV108()
    key = verify_receipt_authorization_v108(receipt, keyring=verified, observed_at=NOW, replay_ledger=ledger)
    assert key.purpose == SigningPurposeV108.EXECUTOR_RECEIPT
    with pytest.raises(SignatureReplayErrorV108):
        verify_receipt_authorization_v108(receipt, keyring=verified, observed_at=NOW, replay_ledger=ledger)


@pytest.mark.parametrize(
    "case",
    [
        "naive-time",
        "invalid-id",
        "invalid-domain",
        "invalid-digest",
        "invalid-public-key",
        "zero-key-generation",
        "invalid-key-interval",
        "revocation-before-validity",
        "empty-keyring",
        "duplicate-keyring-key",
        "future-key-generation",
        "untrusted-root",
        "invalid-root-length",
        "future-keyring",
        "expired-keyring",
        "negative-previous-generation",
        "negative-clock-skew",
    ],
)
def test_validation_edges_fail_closed(case: str) -> None:
    from dataclasses import replace
    from datetime import datetime
    from app.runtime.signing_authority_v108 import (
        RootSignedKeyringSnapshotV108,
        SigningKeyDescriptorV108,
        SigningValidationErrorV108,
        verify_keyring_snapshot_v108,
    )

    root, _, descriptors, snapshot, _ = authority_fixture()
    release = descriptors[0]
    if case == "naive-time":
        with pytest.raises(SigningValidationErrorV108):
            replace(release, not_before=datetime(2026, 8, 6))
    elif case == "invalid-id":
        with pytest.raises(SigningValidationErrorV108):
            replace(release, key_id="bad key")
    elif case == "invalid-domain":
        with pytest.raises(SigningValidationErrorV108):
            replace(authorization_bundle()[-1].release, domain="BAD DOMAIN")
    elif case == "invalid-digest":
        with pytest.raises(SigningValidationErrorV108):
            replace(authorization_bundle()[-1], command_digest="bad")
    elif case == "invalid-public-key":
        with pytest.raises(SigningValidationErrorV108):
            replace(release, public_key_b64="AA==")
    elif case == "zero-key-generation":
        with pytest.raises(SigningValidationErrorV108):
            replace(release, generation=0)
    elif case == "invalid-key-interval":
        with pytest.raises(SigningValidationErrorV108):
            replace(release, not_after=release.not_before)
    elif case == "revocation-before-validity":
        with pytest.raises(SigningValidationErrorV108):
            replace(release, revoked_at=release.not_before - timedelta(seconds=1))
    elif case == "empty-keyring":
        with pytest.raises(SigningValidationErrorV108):
            RootSignedKeyringSnapshotV108(
                generation=1,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=1),
                root_key_id=root.key_id,
                keys=(),
                root_signature_b64=base64.b64encode(b"x" * 64).decode(),
            )
    elif case == "duplicate-keyring-key":
        with pytest.raises(SigningValidationErrorV108):
            replace(snapshot, keys=(release, release))
    elif case == "future-key-generation":
        with pytest.raises(SigningValidationErrorV108):
            replace(snapshot, keys=(replace(release, generation=2),) + descriptors[1:])
    elif case == "untrusted-root":
        with pytest.raises(KeyringVerificationErrorV108):
            verify_keyring_snapshot_v108(snapshot, trusted_root_public_keys={}, previous_generation=0, observed_at=NOW)
    elif case == "invalid-root-length":
        with pytest.raises(SigningValidationErrorV108):
            verify_keyring_snapshot_v108(
                snapshot, trusted_root_public_keys={root.key_id: b"x"}, previous_generation=0, observed_at=NOW
            )
    elif case == "future-keyring":
        future = RootSignedKeyringSnapshotV108.sign(
            generation=2,
            issued_at=NOW + timedelta(minutes=2),
            expires_at=NOW + timedelta(minutes=3),
            keys=descriptors,
            root_provider=root,
        )
        with pytest.raises(KeyringVerificationErrorV108, match="not yet valid"):
            verify_keyring_snapshot_v108(
                future,
                trusted_root_public_keys={root.key_id: root.public_key_bytes()},
                previous_generation=1,
                observed_at=NOW,
            )
    elif case == "expired-keyring":
        expired = RootSignedKeyringSnapshotV108.sign(
            generation=2,
            issued_at=NOW - timedelta(minutes=3),
            expires_at=NOW - timedelta(minutes=2),
            keys=descriptors,
            root_provider=root,
        )
        with pytest.raises(KeyringVerificationErrorV108, match="expired"):
            verify_keyring_snapshot_v108(
                expired,
                trusted_root_public_keys={root.key_id: root.public_key_bytes()},
                previous_generation=1,
                observed_at=NOW,
            )
    elif case == "negative-previous-generation":
        with pytest.raises(SigningValidationErrorV108):
            verify_keyring_snapshot_v108(
                snapshot,
                trusted_root_public_keys={root.key_id: root.public_key_bytes()},
                previous_generation=-1,
                observed_at=NOW,
            )
    elif case == "negative-clock-skew":
        with pytest.raises(SigningValidationErrorV108):
            verify_keyring_snapshot_v108(
                snapshot,
                trusted_root_public_keys={root.key_id: root.public_key_bytes()},
                previous_generation=0,
                observed_at=NOW,
                max_clock_skew_seconds=-1,
            )


def test_low_level_encoding_and_canonical_validation_edges() -> None:
    from dataclasses import dataclass
    from app.runtime.signing_authority_v108 import (
        SigningValidationErrorV108,
        _decode_b64,
        _encode_b64,
        canonical_bytes_v108,
    )

    with pytest.raises(SigningValidationErrorV108):
        _decode_b64(123, expected_length=32, name="test")
    with pytest.raises(SigningValidationErrorV108):
        _decode_b64("%%%", expected_length=32, name="test")
    with pytest.raises(SigningValidationErrorV108, match="length"):
        _decode_b64("AA==", expected_length=32, name="test")
    with pytest.raises(SigningValidationErrorV108):
        _encode_b64(b"x", expected_length=32, name="test")

    @dataclass
    class Plain:
        value: int

    assert canonical_bytes_v108(Plain(1)) == b'{"value":1}'
    with pytest.raises(TypeError):
        canonical_bytes_v108({"bad": object()})


def test_root_provider_boundary_and_signature_are_checked() -> None:
    from app.runtime.signing_authority_v108 import SigningValidationErrorV108

    root, _, descriptors, _, _ = authority_fixture()

    class WrongBackend:
        key_id = root.key_id
        backend = "LOCAL"
        generation = root.generation

        def public_key_bytes(self):
            return root.public_key_bytes()

        def sign(self, payload):
            return root.sign(payload)

    with pytest.raises(SigningValidationErrorV108, match="KMS or HSM"):
        RootSignedKeyringSnapshotV108.sign(
            generation=1,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            keys=descriptors,
            root_provider=WrongBackend(),
        )

    class BrokenRoot:
        key_id = root.key_id
        backend = root.backend
        generation = root.generation

        def public_key_bytes(self):
            return root.public_key_bytes()

        def sign(self, payload):
            return b"z" * 64

    with pytest.raises(SignatureVerificationErrorV108, match="root provider"):
        RootSignedKeyringSnapshotV108.sign(
            generation=1,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            keys=descriptors,
            root_provider=BrokenRoot(),
        )


def test_verified_keyring_rejects_unknown_wrong_generation_purpose_and_inactive_keys() -> None:
    from app.runtime.signing_authority_v108 import VerifiedKeyringV108

    _, _, descriptors, _, verified = authority_fixture()
    with pytest.raises(SignatureVerificationErrorV108, match="unknown"):
        verified.require_key(
            key_id="missing",
            purpose=SigningPurposeV108.RELEASE_APPROVAL,
            key_generation=1,
            observed_at=NOW,
        )
    with pytest.raises(SignatureVerificationErrorV108, match="purpose"):
        verified.require_key(
            key_id=descriptors[0].key_id,
            purpose=SigningPurposeV108.RISK_APPROVAL,
            key_generation=1,
            observed_at=NOW,
        )
    with pytest.raises(SignatureVerificationErrorV108, match="generation"):
        verified.require_key(
            key_id=descriptors[0].key_id,
            purpose=SigningPurposeV108.RELEASE_APPROVAL,
            key_generation=2,
            observed_at=NOW,
        )
    with pytest.raises(SignatureVerificationErrorV108, match="inactive"):
        verified.require_key(
            key_id=descriptors[0].key_id,
            purpose=SigningPurposeV108.RELEASE_APPROVAL,
            key_generation=1,
            observed_at=NOW + timedelta(hours=2),
        )
    with pytest.raises(Exception):
        VerifiedKeyringV108(generation=1, snapshot_digest="bad", keys={})


def test_envelope_construction_signing_and_verification_edges() -> None:
    from dataclasses import replace
    from app.runtime.signing_authority_v108 import SigningValidationErrorV108

    _, providers, descriptors, _, verified = authority_fixture()
    release = providers[0]
    release_d = descriptors[0]
    valid = sign_envelope_v108(
        provider=release,
        descriptor=release_d,
        keyring_generation=1,
        signature_id="sig-edge",
        purpose=SigningPurposeV108.RELEASE_APPROVAL,
        domain="astra.rollout.authorization.v108",
        payload_digest="1" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        nonce="nonce-edge",
    )
    assert len(valid.envelope_digest) == 64

    with pytest.raises(SigningValidationErrorV108, match="generations"):
        replace(valid, key_generation=0)
    with pytest.raises(SigningValidationErrorV108, match="interval"):
        replace(valid, expires_at=valid.issued_at)

    class WrongGeneration:
        key_id = release.key_id
        backend = release.backend
        generation = 2

        def public_key_bytes(self):
            return release.public_key_bytes()

        def sign(self, payload):
            return release.sign(payload)

    with pytest.raises(SigningValidationErrorV108, match="provider generation"):
        sign_envelope_v108(
            provider=WrongGeneration(), descriptor=release_d, keyring_generation=1,
            signature_id="sig-g", purpose=SigningPurposeV108.RELEASE_APPROVAL,
            domain="astra.rollout.authorization.v108", payload_digest="1" * 64,
            issued_at=NOW, expires_at=NOW + timedelta(seconds=30), nonce="nonce-g"
        )

    other = LocalProviderV108.create("release-key", release.backend, generation=1)
    class WrongPublic:
        key_id = release.key_id
        backend = release.backend
        generation = release.generation
        def public_key_bytes(self): return other.public_key_bytes()
        def sign(self, payload): return other.sign(payload)
    with pytest.raises(SigningValidationErrorV108, match="public key"):
        sign_envelope_v108(
            provider=WrongPublic(), descriptor=release_d, keyring_generation=1,
            signature_id="sig-pub", purpose=SigningPurposeV108.RELEASE_APPROVAL,
            domain="astra.rollout.authorization.v108", payload_digest="1" * 64,
            issued_at=NOW, expires_at=NOW + timedelta(seconds=30), nonce="nonce-pub"
        )

    with pytest.raises(SigningValidationErrorV108, match="purpose"):
        sign_envelope_v108(
            provider=release, descriptor=release_d, keyring_generation=1,
            signature_id="sig-purpose", purpose=SigningPurposeV108.RISK_APPROVAL,
            domain="astra.rollout.authorization.v108", payload_digest="1" * 64,
            issued_at=NOW, expires_at=NOW + timedelta(seconds=30), nonce="nonce-purpose"
        )
    with pytest.raises(SigningValidationErrorV108, match="lifetime"):
        sign_envelope_v108(
            provider=release, descriptor=release_d, keyring_generation=1,
            signature_id="sig-life", purpose=SigningPurposeV108.RELEASE_APPROVAL,
            domain="astra.rollout.authorization.v108", payload_digest="1" * 64,
            issued_at=NOW, expires_at=NOW + timedelta(seconds=31), nonce="nonce-life",
            max_lifetime_seconds=30,
        )
    with pytest.raises(SigningValidationErrorV108, match="key validity"):
        sign_envelope_v108(
            provider=release, descriptor=release_d, keyring_generation=1,
            signature_id="sig-validity", purpose=SigningPurposeV108.RELEASE_APPROVAL,
            domain="astra.rollout.authorization.v108", payload_digest="1" * 64,
            issued_at=NOW + timedelta(hours=2), expires_at=NOW + timedelta(hours=2, seconds=1),
            nonce="nonce-validity"
        )

    with pytest.raises(SignatureVerificationErrorV108, match="purpose"):
        verify_envelope_v108(
            valid, keyring=verified, expected_purpose=SigningPurposeV108.RISK_APPROVAL,
            expected_domain=valid.domain, expected_payload_digest=valid.payload_digest, observed_at=NOW
        )
    with pytest.raises(SignatureVerificationErrorV108, match="keyring generation"):
        verify_envelope_v108(
            replace(valid, keyring_generation=2), keyring=verified,
            expected_purpose=valid.purpose, expected_domain=valid.domain,
            expected_payload_digest=valid.payload_digest, observed_at=NOW
        )
    with pytest.raises(SignatureVerificationErrorV108, match="future"):
        verify_envelope_v108(
            replace(valid, issued_at=NOW + timedelta(minutes=1), expires_at=NOW + timedelta(minutes=2)),
            keyring=verified, expected_purpose=valid.purpose, expected_domain=valid.domain,
            expected_payload_digest=valid.payload_digest, observed_at=NOW
        )
    with pytest.raises(SignatureVerificationErrorV108, match="expired"):
        verify_envelope_v108(
            replace(valid, issued_at=NOW - timedelta(minutes=2), expires_at=NOW - timedelta(minutes=1)),
            keyring=verified, expected_purpose=valid.purpose, expected_domain=valid.domain,
            expected_payload_digest=valid.payload_digest, observed_at=NOW
        )
    raw = bytearray(base64.b64decode(valid.signature_b64)); raw[-1] ^= 1
    with pytest.raises(SignatureVerificationErrorV108, match="invalid Ed25519"):
        verify_envelope_v108(
            replace(valid, signature_b64=base64.b64encode(raw).decode()), keyring=verified,
            expected_purpose=valid.purpose, expected_domain=valid.domain,
            expected_payload_digest=valid.payload_digest, observed_at=NOW
        )


def test_bundle_and_receipt_generation_and_duplicate_replay_edges() -> None:
    from app.runtime.signing_authority_v108 import verify_receipt_authorization_v108
    from tests.helpers_v108 import receipt_authorization

    _, providers, descriptors, _, verified, bundle = authorization_bundle()
    with pytest.raises(Exception, match="keyring_generation"):
        replace(bundle, keyring_generation=0)
    with pytest.raises(SignatureVerificationErrorV108, match="bundle keyring"):
        verify_rollout_authorization_v108(replace(bundle, keyring_generation=2), keyring=verified, observed_at=NOW)
    ledger = SignatureReplayLedgerV108()
    with pytest.raises(SignatureReplayErrorV108, match="duplicate"):
        ledger.consume_many((bundle.release, bundle.release))
    assert ledger.size == 0

    receipt = receipt_authorization(bundle, providers, descriptors)
    with pytest.raises(Exception, match="keyring_generation"):
        replace(receipt, keyring_generation=0)
    with pytest.raises(SignatureVerificationErrorV108, match="receipt keyring"):
        verify_receipt_authorization_v108(replace(receipt, keyring_generation=2), keyring=verified, observed_at=NOW)
