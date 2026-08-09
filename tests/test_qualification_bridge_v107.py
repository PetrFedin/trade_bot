from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

import pytest

from app.runtime.qualification_bridge_v107 import build_execution_intent_from_v106
from app.runtime.rollout_execution_v107 import DeploymentActionV107, ValidationErrorV107
from tests.conftest import NOW


class LegacyAction(str, Enum):
    PROMOTE = "PROMOTE"
    ROLLBACK = "ROLLBACK"


class LegacyStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"


@dataclass
class FakeV106Action:
    action_id: str = "action-001"
    qualification_id: str = "qual-001"
    action: LegacyAction = LegacyAction.PROMOTE
    created_at = NOW
    evidence_digest: str = "3" * 64
    state_digest: str = "4" * 64
    idempotency_key: str = "idem-001"
    status: LegacyStatus = LegacyStatus.PENDING
    attempt_count: int = 0
    valid: bool = True
    signature: str = "6" * 64

    def verify(self, keyring):
        if not self.valid or keyring.get("legacy") != b"x" * 32:
            raise RuntimeError("bad signature")

    def to_payload(self):
        return {
            "action_id": self.action_id,
            "qualification_id": self.qualification_id,
            "action": self.action.value,
            "evidence_digest": self.evidence_digest,
            "state_digest": self.state_digest,
            "idempotency_key": self.idempotency_key,
        }


def test_bridge_creates_unsigned_intent_but_requires_new_attestations(policy, snapshot):
    result = build_execution_intent_from_v106(
        action=FakeV106Action(), action_keyring={"legacy": b"x" * 32}, policy=policy,
        snapshot=snapshot, command_id="cmd-bridge", target_replicas=4,
        issued_at=NOW, not_before=NOW, expires_at=NOW.replace(minute=5),
        fencing_token=12, nonce="bridge-nonce",
    )
    assert result.intent.action == DeploymentActionV107.PROMOTE
    assert result.intent.qualification_evidence_digest == "3" * 64
    assert result.intent.qualification_action_digest == result.source_action_digest
    assert result.requires_independent_release_and_risk_attestations is True


def test_bridge_rejects_unverified_or_claimed_action(policy, snapshot):
    with pytest.raises(ValidationErrorV107, match="verification"):
        build_execution_intent_from_v106(
            action=FakeV106Action(valid=False), action_keyring={"legacy": b"x" * 32}, policy=policy,
            snapshot=snapshot, command_id="cmd-bridge", target_replicas=4,
            issued_at=NOW, not_before=NOW, expires_at=NOW.replace(minute=5), fencing_token=12, nonce="n",
        )
    with pytest.raises(ValidationErrorV107, match="pending"):
        build_execution_intent_from_v106(
            action=FakeV106Action(status=LegacyStatus.CLAIMED), action_keyring={"legacy": b"x" * 32}, policy=policy,
            snapshot=snapshot, command_id="cmd-bridge", target_replicas=4,
            issued_at=NOW, not_before=NOW, expires_at=NOW.replace(minute=5), fencing_token=12, nonce="n",
        )


def test_bridge_rejects_scope_or_routing_drift(policy, snapshot):
    with pytest.raises(ValidationErrorV107, match="snapshot"):
        build_execution_intent_from_v106(
            action=FakeV106Action(), action_keyring={"legacy": b"x" * 32}, policy=policy,
            snapshot=replace(snapshot, deployment_uid="other"), command_id="cmd-bridge", target_replicas=4,
            issued_at=NOW, not_before=NOW, expires_at=NOW.replace(minute=5), fencing_token=12, nonce="n",
        )
    with pytest.raises(ValidationErrorV107, match="routing"):
        build_execution_intent_from_v106(
            action=FakeV106Action(), action_keyring={"legacy": b"x" * 32}, policy=policy,
            snapshot=replace(snapshot, live_trading_allowed=True), command_id="cmd-bridge", target_replicas=4,
            issued_at=NOW, not_before=NOW, expires_at=NOW.replace(minute=5), fencing_token=12, nonce="n",
        )
