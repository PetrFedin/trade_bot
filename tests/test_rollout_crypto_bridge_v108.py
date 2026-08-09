from __future__ import annotations

from dataclasses import replace

import pytest

from app.runtime.rollout_crypto_bridge_v108 import verify_v107_rollout_command_v108
from app.runtime.signing_authority_v108 import SignatureReplayLedgerV108
from tests.helpers_v108 import NOW, authorization_bundle


class DummyCommand:
    command_digest = "1" * 64

    def __init__(self) -> None:
        self.calls = 0

    def verify(self, **kwargs) -> None:
        self.calls += 1
        assert kwargs["enforce_validity"] is True
        assert kwargs["now"] == NOW


class DummyPolicy:
    policy_digest = "2" * 64


def test_bridge_requires_v107_and_v108_gates() -> None:
    root, _, _, snapshot, _, bundle = authorization_bundle()
    command = DummyCommand()
    predecessor_calls: list[str] = []
    result = verify_v107_rollout_command_v108(
        command=command,
        policy=DummyPolicy(),
        bundle=bundle,
        keyring_snapshot=snapshot,
        trusted_root_public_keys={root.key_id: root.public_key_bytes()},
        previous_keyring_generation=0,
        observed_at=NOW,
        approval_keyring_v107={"legacy": b"x" * 32},
        controller_keyring_v107={"legacy": b"y" * 32},
        replay_ledger=SignatureReplayLedgerV108(),
        predecessor_verifier=lambda: predecessor_calls.append("ok"),
    )
    assert command.calls == 1
    assert predecessor_calls == ["ok"]
    assert result.command_digest == command.command_digest
    assert result.authorization_bundle_digest == bundle.bundle_digest


def test_bridge_rejects_command_or_policy_substitution() -> None:
    root, _, _, snapshot, _, bundle = authorization_bundle()
    command = DummyCommand()
    command.command_digest = "9" * 64
    with pytest.raises(ValueError, match="command digest"):
        verify_v107_rollout_command_v108(
            command=command,
            policy=DummyPolicy(),
            bundle=bundle,
            keyring_snapshot=snapshot,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW,
            approval_keyring_v107={},
            controller_keyring_v107={},
        )

    command.command_digest = bundle.command_digest
    bad_policy = DummyPolicy()
    bad_policy.policy_digest = "8" * 64
    with pytest.raises(ValueError, match="policy digest"):
        verify_v107_rollout_command_v108(
            command=command,
            policy=bad_policy,
            bundle=bundle,
            keyring_snapshot=snapshot,
            trusted_root_public_keys={root.key_id: root.public_key_bytes()},
            previous_keyring_generation=0,
            observed_at=NOW,
            approval_keyring_v107={},
            controller_keyring_v107={},
        )


def test_receipt_bridge_binds_receipt_command_and_bundle() -> None:
    from app.runtime.rollout_crypto_bridge_v108 import verify_v107_rollout_receipt_v108
    from tests.helpers_v108 import receipt_authorization

    _, providers, descriptors, _, verified, bundle = authorization_bundle()
    authorization = receipt_authorization(bundle, providers, descriptors)

    class Receipt:
        receipt_digest = "4" * 64

    result = verify_v107_rollout_receipt_v108(
        receipt=Receipt(),
        command_digest=bundle.command_digest,
        bundle=bundle,
        receipt_authorization=authorization,
        keyring=verified,
        observed_at=NOW,
        replay_ledger=SignatureReplayLedgerV108(),
    )
    assert result.executor_key_id == "executor-key"

    class WrongReceipt:
        receipt_digest = "5" * 64

    with pytest.raises(ValueError, match="V107 receipt"):
        verify_v107_rollout_receipt_v108(
            receipt=WrongReceipt(),
            command_digest=bundle.command_digest,
            bundle=bundle,
            receipt_authorization=authorization,
            keyring=verified,
            observed_at=NOW,
        )


def test_receipt_bridge_rejects_command_and_bundle_substitution() -> None:
    from app.runtime.rollout_crypto_bridge_v108 import verify_v107_rollout_receipt_v108
    from tests.helpers_v108 import receipt_authorization

    _, providers, descriptors, _, verified, bundle = authorization_bundle()
    authorization = receipt_authorization(bundle, providers, descriptors)

    class Receipt:
        receipt_digest = authorization.receipt_digest

    with pytest.raises(ValueError, match="V107 command"):
        verify_v107_rollout_receipt_v108(
            receipt=Receipt(), command_digest="9" * 64, bundle=bundle,
            receipt_authorization=authorization, keyring=verified, observed_at=NOW
        )

    class OtherBundle:
        bundle_digest = "8" * 64

    with pytest.raises(ValueError, match="authorization bundle"):
        verify_v107_rollout_receipt_v108(
            receipt=Receipt(), command_digest=bundle.command_digest, bundle=OtherBundle(),
            receipt_authorization=authorization, keyring=verified, observed_at=NOW
        )
