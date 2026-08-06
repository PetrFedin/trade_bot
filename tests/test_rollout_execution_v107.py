from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.runtime.rollout_execution_v107 import (
    ApprovalAttestationV107,
    ApprovalRoleV107,
    DeploymentActionV107,
    DeploymentExecutionCoordinatorV107,
    DeploymentExecutionIntentV107,
    DeploymentExecutionPolicyV107,
    DeploymentRuntimeSnapshotV107,
    ExecutionGateSetV107,
    ExecutionGateV107,
    ExecutionJournalV107,
    ExecutionReceiptV107,
    ExecutionReplayLedgerV107,
    ReceiptStatusV107,
    ReplayErrorV107,
    SignatureErrorV107,
    SignedDeploymentExecutionCommandV107,
    StateTransitionErrorV107,
    ValidationErrorV107,
    certify_full_rollout_v107,
    digest_v107,
    evaluate_execution_preflight_v107,
)
from tests.conftest import (
    CONFIG,
    CONTROLLER_SECRET,
    EXECUTOR_SECRET,
    IMAGE,
    NOW,
    RELEASE_SECRET,
    RISK_SECRET,
)


def test_command_verifies_with_dual_control(policy, command):
    command.verify(
        policy=policy,
        approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
        controller_keyring={"controller-key": CONTROLLER_SECRET},
        now=NOW + timedelta(seconds=1),
    )


def test_command_rejects_expired(policy, command):
    with pytest.raises(SignatureErrorV107, match="expired"):
        command.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={"controller-key": CONTROLLER_SECRET},
            now=NOW + timedelta(minutes=6),
        )


def test_command_rejects_same_approver(policy, intent):
    release = ApprovalAttestationV107.sign(
        approval_id="a1", intent=intent, approver_id="same", role=ApprovalRoleV107.RELEASE,
        signed_at=NOW, nonce="n1", key_id="release-key", secret=RELEASE_SECRET,
    )
    risk = ApprovalAttestationV107.sign(
        approval_id="a2", intent=intent, approver_id="same", role=ApprovalRoleV107.RISK,
        signed_at=NOW, nonce="n2", key_id="risk-key", secret=RISK_SECRET,
    )
    command = SignedDeploymentExecutionCommandV107.sign(
        intent=intent, approvals=(release, risk), controller_key_id="controller-key", controller_secret=CONTROLLER_SECRET,
    )
    with pytest.raises(SignatureErrorV107, match="distinct"):
        command.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={"controller-key": CONTROLLER_SECRET},
            now=NOW,
        )


def test_command_rejects_duplicate_role(policy, intent):
    one = ApprovalAttestationV107.sign(
        approval_id="a1", intent=intent, approver_id="alice", role=ApprovalRoleV107.RELEASE,
        signed_at=NOW, nonce="n1", key_id="release-key", secret=RELEASE_SECRET,
    )
    two = ApprovalAttestationV107.sign(
        approval_id="a2", intent=intent, approver_id="bob", role=ApprovalRoleV107.RELEASE,
        signed_at=NOW, nonce="n2", key_id="risk-key", secret=RISK_SECRET,
    )
    command = SignedDeploymentExecutionCommandV107.sign(
        intent=intent, approvals=(one, two), controller_key_id="controller-key", controller_secret=CONTROLLER_SECRET,
    )
    with pytest.raises(SignatureErrorV107, match="release and risk"):
        command.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={"controller-key": CONTROLLER_SECRET},
            now=NOW,
        )


def test_approval_tamper_is_rejected(policy, command):
    bad = replace(command.approvals[0], approver_id="mallory")
    tampered = replace(command, approvals=(bad, command.approvals[1]))
    with pytest.raises(SignatureErrorV107, match="signature mismatch"):
        tampered.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={"controller-key": CONTROLLER_SECRET},
            now=NOW,
        )


def test_controller_tamper_is_rejected(policy, command):
    tampered = replace(command, controller_signature="0" * 64)
    with pytest.raises(SignatureErrorV107, match="controller signature"):
        tampered.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={"controller-key": CONTROLLER_SECRET},
            now=NOW,
        )


def test_future_approval_rejected(policy, intent):
    release = ApprovalAttestationV107.sign(
        approval_id="a1", intent=intent, approver_id="alice", role=ApprovalRoleV107.RELEASE,
        signed_at=NOW + timedelta(seconds=30), nonce="n1", key_id="release-key", secret=RELEASE_SECRET,
    )
    risk = ApprovalAttestationV107.sign(
        approval_id="a2", intent=intent, approver_id="bob", role=ApprovalRoleV107.RISK,
        signed_at=NOW, nonce="n2", key_id="risk-key", secret=RISK_SECRET,
    )
    command = SignedDeploymentExecutionCommandV107.sign(
        intent=intent, approvals=(release, risk), controller_key_id="controller-key", controller_secret=CONTROLLER_SECRET,
    )
    with pytest.raises(SignatureErrorV107, match="future"):
        command.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={"controller-key": CONTROLLER_SECRET},
            now=NOW,
        )


def test_replay_ledger_blocks_reuse(policy, command):
    ledger = ExecutionReplayLedgerV107()
    kwargs = dict(
        policy=policy,
        approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
        controller_keyring={"controller-key": CONTROLLER_SECRET},
        now=NOW,
        replay_ledger=ledger,
    )
    command.verify(**kwargs)
    assert len(ledger) == 1
    with pytest.raises(ReplayErrorV107):
        command.verify(**kwargs)


def test_policy_rejects_live_boundaries():
    with pytest.raises(ValidationErrorV107, match="cannot enable"):
        DeploymentExecutionPolicyV107(
            cluster="c", namespace="n", deployment_name="d", deployment_uid="u", service_account="s",
            expected_image_digest=IMAGE, expected_config_digest=CONFIG, min_replicas=1, max_replicas=2,
            rollback_replicas=0, live_trading_allowed=True,
        )


def test_intent_rejects_non_increasing_promotion(intent):
    with pytest.raises(ValidationErrorV107, match="increase"):
        replace(intent, target_replicas=intent.expected_current_replicas)


def test_rollback_policy_is_exact(policy, intent):
    rollback = replace(
        intent,
        action=DeploymentActionV107.ROLLBACK,
        expected_current_replicas=4,
        target_replicas=1,
    )
    rollback.verify_policy(policy)
    with pytest.raises(ValidationErrorV107, match="rollback target"):
        replace(rollback, target_replicas=0).verify_policy(policy)


def test_partial_marker_is_rejected(snapshot):
    with pytest.raises(ValidationErrorV107, match="entirely absent or complete"):
        replace(snapshot, action_id_annotation="action-001")


def test_preflight_passes(policy, command, snapshot):
    gates = evaluate_execution_preflight_v107(policy=policy, command=command, snapshot=snapshot)
    assert gates.passed
    assert not gates.already_applied


def test_preflight_rejects_stale_resource_version(policy, command, snapshot):
    gates = evaluate_execution_preflight_v107(
        policy=policy, command=command, snapshot=replace(snapshot, resource_version="101")
    )
    assert not gates.passed
    assert not next(g for g in gates.gates if g.name == "resource_version").passed


def test_preflight_allows_newer_fence_after_complete_prior_marker(policy, command, snapshot):
    prior = replace(
        snapshot,
        action_id_annotation="older-action",
        command_digest_annotation="a" * 64,
        fencing_token_annotation=10,
        target_replicas_annotation=3,
    )
    assert evaluate_execution_preflight_v107(policy=policy, command=command, snapshot=prior).passed


def test_preflight_blocks_same_or_newer_conflicting_fence(policy, command, snapshot):
    conflict = replace(
        snapshot,
        action_id_annotation="other-action",
        command_digest_annotation="a" * 64,
        fencing_token_annotation=11,
        target_replicas_annotation=3,
    )
    gates = evaluate_execution_preflight_v107(policy=policy, command=command, snapshot=conflict)
    assert not gates.passed
    assert not next(g for g in gates.gates if g.name == "fencing").passed


def test_already_applied_bypasses_original_lock_but_not_identity(policy, command, snapshot):
    applied = replace(
        snapshot,
        resource_version="999",
        generation=8,
        replicas=4,
        ready_replicas=4,
        available_replicas=4,
        action_id_annotation=command.intent.action_id,
        command_digest_annotation=command.command_digest,
        fencing_token_annotation=command.intent.fencing_token,
        target_replicas_annotation=command.intent.target_replicas,
    )
    gates = evaluate_execution_preflight_v107(policy=policy, command=command, snapshot=applied)
    assert gates.passed and gates.already_applied
    bad = replace(applied, image_digest="sha256:" + "9" * 64)
    assert not evaluate_execution_preflight_v107(policy=policy, command=command, snapshot=bad).passed


def test_full_rollout_requires_every_replica_ready(policy, command, snapshot):
    post = replace(
        snapshot,
        replicas=4,
        ready_replicas=2,
        available_replicas=4,
        action_id_annotation=command.intent.action_id,
        command_digest_annotation=command.command_digest,
        fencing_token_annotation=command.intent.fencing_token,
        target_replicas_annotation=command.intent.target_replicas,
    )
    gates = certify_full_rollout_v107(policy=policy, command=command, snapshot=post)
    assert not gates.passed
    assert not next(g for g in gates.gates if g.name == "all_ready").passed
    assert certify_full_rollout_v107(policy=policy, command=command, snapshot=replace(post, ready_replicas=4)).passed


def test_receipt_signature_and_invariants(command, snapshot):
    receipt = ExecutionReceiptV107.sign(
        receipt_id="r1", command=command, worker_id="worker-1", status=ReceiptStatusV107.APPLIED,
        observed_at=NOW, pre_snapshot_digest=snapshot.snapshot_digest,
        post_snapshot_digest="f" * 64, patch_digest="e" * 64, mutation_attempted=True,
        reason="done", executor_key_id="executor-key", executor_secret=EXECUTOR_SECRET,
    )
    receipt.verify({"executor-key": EXECUTOR_SECRET})
    with pytest.raises(ValidationErrorV107, match="post snapshot"):
        replace(receipt, post_snapshot_digest=None)
    with pytest.raises(SignatureErrorV107):
        replace(receipt, signature="0" * 64).verify({"executor-key": EXECUTOR_SECRET})


def test_journal_chain_detects_tampering():
    journal = ExecutionJournalV107()
    journal.append("ONE", {"x": 1}, NOW)
    journal.append("TWO", {"x": 2}, NOW + timedelta(seconds=1))
    assert journal.verify()
    journal._events[1] = replace(journal._events[1], previous_digest="f" * 64)
    assert not journal.verify()


def test_coordinator_enforces_single_mutation(policy, command, snapshot):
    c = DeploymentExecutionCoordinatorV107(worker_id="worker-1", policy=policy, command=command)
    c.claim(NOW)
    gates = evaluate_execution_preflight_v107(policy=policy, command=command, snapshot=snapshot)
    c.record_preflight(gates, NOW)
    c.start_mutation("a" * 64, NOW)
    with pytest.raises(StateTransitionErrorV107):
        c.start_mutation("b" * 64, NOW)
    assert c.mutation_attempts == 1


def test_gate_set_rejects_duplicate_names():
    gate = ExecutionGateV107("same", True, "ok", digest_v107({"x": 1}))
    with pytest.raises(ValidationErrorV107, match="duplicate"):
        ExecutionGateSetV107((gate, gate))
