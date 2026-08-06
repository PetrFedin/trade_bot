from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from app.runtime.signing_authority_v108 import (
    RootSignedKeyringSnapshotV108,
    RolloutAuthorizationBundleV108,
    SignatureReplayLedgerV108,
    VerifiedKeyringV108,
    verify_keyring_snapshot_v108,
    verify_rollout_authorization_v108,
)


@dataclass(frozen=True, slots=True)
class VerifiedRolloutAdmissionV108:
    command_digest: str
    policy_digest: str
    authorization_bundle_digest: str
    keyring_generation: int
    keyring_snapshot_digest: str


def verify_v107_rollout_command_v108(
    *,
    command: Any,
    policy: Any,
    bundle: RolloutAuthorizationBundleV108,
    keyring_snapshot: RootSignedKeyringSnapshotV108,
    trusted_root_public_keys: Mapping[str, bytes],
    previous_keyring_generation: int,
    observed_at: datetime,
    approval_keyring_v107: Mapping[str, bytes],
    controller_keyring_v107: Mapping[str, bytes],
    replay_ledger: SignatureReplayLedgerV108 | None = None,
    predecessor_verifier: Callable[[], None] | None = None,
) -> VerifiedRolloutAdmissionV108:
    """Require the complete V107 gate and a separate asymmetric V108 authorization.

    The V107 HMAC command is treated only as a predecessor compatibility gate. It
    is not sufficient to authorize admission after this bridge is installed.
    """

    if predecessor_verifier is not None:
        predecessor_verifier()
    command.verify(
        policy=policy,
        approval_keyring=approval_keyring_v107,
        controller_keyring=controller_keyring_v107,
        now=observed_at,
        enforce_validity=True,
    )
    if command.command_digest != bundle.command_digest:
        raise ValueError("V108 bundle is not bound to V107 command digest")
    if policy.policy_digest != bundle.policy_digest:
        raise ValueError("V108 bundle is not bound to V107 policy digest")
    verified: VerifiedKeyringV108 = verify_keyring_snapshot_v108(
        keyring_snapshot,
        trusted_root_public_keys=trusted_root_public_keys,
        previous_generation=previous_keyring_generation,
        observed_at=observed_at,
    )
    verify_rollout_authorization_v108(
        bundle,
        keyring=verified,
        observed_at=observed_at,
        replay_ledger=replay_ledger,
    )
    return VerifiedRolloutAdmissionV108(
        command_digest=command.command_digest,
        policy_digest=policy.policy_digest,
        authorization_bundle_digest=bundle.bundle_digest,
        keyring_generation=verified.generation,
        keyring_snapshot_digest=verified.snapshot_digest,
    )


@dataclass(frozen=True, slots=True)
class VerifiedRolloutReceiptV108:
    receipt_digest: str
    command_digest: str
    receipt_authorization_digest: str
    executor_key_id: str
    keyring_generation: int


def verify_v107_rollout_receipt_v108(
    *,
    receipt: Any,
    command_digest: str,
    bundle: Any,
    receipt_authorization: Any,
    keyring: VerifiedKeyringV108,
    observed_at: datetime,
    replay_ledger: SignatureReplayLedgerV108 | None = None,
) -> VerifiedRolloutReceiptV108:
    from app.runtime.signing_authority_v108 import verify_receipt_authorization_v108

    if receipt.receipt_digest != receipt_authorization.receipt_digest:
        raise ValueError("V108 receipt authorization is not bound to V107 receipt")
    if command_digest != receipt_authorization.command_digest:
        raise ValueError("V108 receipt authorization is not bound to V107 command")
    if bundle.bundle_digest != receipt_authorization.authorization_bundle_digest:
        raise ValueError("V108 receipt authorization is not bound to authorization bundle")
    descriptor = verify_receipt_authorization_v108(
        receipt_authorization,
        keyring=keyring,
        observed_at=observed_at,
        replay_ledger=replay_ledger,
    )
    return VerifiedRolloutReceiptV108(
        receipt_digest=receipt.receipt_digest,
        command_digest=command_digest,
        receipt_authorization_digest=receipt_authorization.authorization_digest,
        executor_key_id=descriptor.key_id,
        keyring_generation=keyring.generation,
    )
