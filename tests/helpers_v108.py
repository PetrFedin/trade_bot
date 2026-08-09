from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.runtime.signing_authority_v108 import (
    RootSignedKeyringSnapshotV108,
    RolloutAuthorizationBundleV108,
    SigningBackendV108,
    SigningKeyDescriptorV108,
    SigningPurposeV108,
    authorization_payload_digest_v108,
    sign_envelope_v108,
    verify_keyring_snapshot_v108,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 6, 9, 30, tzinfo=UTC)


@dataclass(slots=True)
class LocalProviderV108:
    key_id: str
    backend: SigningBackendV108
    generation: int
    _private: Ed25519PrivateKey

    @classmethod
    def create(
        cls, key_id: str, backend: SigningBackendV108, generation: int = 1
    ) -> "LocalProviderV108":
        return cls(key_id, backend, generation, Ed25519PrivateKey.generate())

    def public_key_bytes(self) -> bytes:
        return self._private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def sign(self, payload: bytes) -> bytes:
        return self._private.sign(payload)


def descriptor(
    provider: LocalProviderV108,
    *,
    owner: str,
    purpose: SigningPurposeV108,
    revoked_at: datetime | None = None,
) -> SigningKeyDescriptorV108:
    from base64 import b64encode

    return SigningKeyDescriptorV108(
        key_id=provider.key_id,
        owner_id=owner,
        purpose=purpose,
        backend=provider.backend,
        generation=provider.generation,
        public_key_b64=b64encode(provider.public_key_bytes()).decode("ascii"),
        not_before=NOW - timedelta(hours=1),
        not_after=NOW + timedelta(hours=1),
        revoked_at=revoked_at,
    )


def authority_fixture(*, risk_owner: str = "risk-owner"):
    root = LocalProviderV108.create("root-key", SigningBackendV108.HSM)
    release = LocalProviderV108.create("release-key", SigningBackendV108.HSM)
    risk = LocalProviderV108.create("risk-key", SigningBackendV108.KMS)
    controller = LocalProviderV108.create("controller-key", SigningBackendV108.HSM)
    executor = LocalProviderV108.create("executor-key", SigningBackendV108.KMS)
    descriptors = (
        descriptor(release, owner="release-owner", purpose=SigningPurposeV108.RELEASE_APPROVAL),
        descriptor(risk, owner=risk_owner, purpose=SigningPurposeV108.RISK_APPROVAL),
        descriptor(controller, owner="controller-owner", purpose=SigningPurposeV108.CONTROLLER_COMMAND),
        descriptor(executor, owner="executor-owner", purpose=SigningPurposeV108.EXECUTOR_RECEIPT),
    )
    snapshot = RootSignedKeyringSnapshotV108.sign(
        generation=1,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=30),
        keys=descriptors,
        root_provider=root,
    )
    verified = verify_keyring_snapshot_v108(
        snapshot,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_generation=0,
        observed_at=NOW,
    )
    return root, (release, risk, controller, executor), descriptors, snapshot, verified


def authorization_bundle(*, risk_owner: str = "risk-owner"):
    root, providers, descriptors, snapshot, verified = authority_fixture(risk_owner=risk_owner)
    release, risk, controller, _ = providers
    release_d, risk_d, controller_d, _ = descriptors
    command_digest = "1" * 64
    policy_digest = "2" * 64
    predecessor_digest = "3" * 64
    payload = authorization_payload_digest_v108(
        command_digest=command_digest,
        policy_digest=policy_digest,
        predecessor_release_identity_digest=predecessor_digest,
    )
    kwargs = dict(
        keyring_generation=1,
        domain="astra.rollout.authorization.v108",
        payload_digest=payload,
        issued_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=2),
    )
    bundle = RolloutAuthorizationBundleV108(
        bundle_id="bundle-1",
        command_digest=command_digest,
        policy_digest=policy_digest,
        predecessor_release_identity_digest=predecessor_digest,
        keyring_generation=1,
        release=sign_envelope_v108(
            provider=release,
            descriptor=release_d,
            signature_id="sig-release",
            purpose=SigningPurposeV108.RELEASE_APPROVAL,
            nonce="nonce-release",
            **kwargs,
        ),
        risk=sign_envelope_v108(
            provider=risk,
            descriptor=risk_d,
            signature_id="sig-risk",
            purpose=SigningPurposeV108.RISK_APPROVAL,
            nonce="nonce-risk",
            **kwargs,
        ),
        controller=sign_envelope_v108(
            provider=controller,
            descriptor=controller_d,
            signature_id="sig-controller",
            purpose=SigningPurposeV108.CONTROLLER_COMMAND,
            nonce="nonce-controller",
            **kwargs,
        ),
    )
    return root, providers, descriptors, snapshot, verified, bundle


def receipt_authorization(bundle, providers, descriptors):
    from app.runtime.signing_authority_v108 import (
        ReceiptAuthorizationV108,
        receipt_payload_digest_v108,
        sign_envelope_v108,
    )

    executor = providers[3]
    executor_d = descriptors[3]
    receipt_digest = "4" * 64
    payload = receipt_payload_digest_v108(
        receipt_digest=receipt_digest,
        command_digest=bundle.command_digest,
        authorization_bundle_digest=bundle.bundle_digest,
    )
    envelope = sign_envelope_v108(
        provider=executor,
        descriptor=executor_d,
        keyring_generation=1,
        signature_id="sig-executor",
        purpose=SigningPurposeV108.EXECUTOR_RECEIPT,
        domain="astra.rollout.receipt.v108",
        payload_digest=payload,
        issued_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=2),
        nonce="nonce-executor",
    )
    return ReceiptAuthorizationV108(
        receipt_id="receipt-auth-1",
        receipt_digest=receipt_digest,
        command_digest=bundle.command_digest,
        authorization_bundle_digest=bundle.bundle_digest,
        keyring_generation=1,
        executor=envelope,
    )
