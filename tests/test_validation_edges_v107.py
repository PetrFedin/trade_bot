from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.runtime.rollout_execution_v107 import (
    ApprovalAttestationV107,
    ApprovalRoleV107,
    DeploymentActionV107,
    DeploymentExecutionCoordinatorV107,
    DeploymentExecutionPolicyV107,
    DeploymentRuntimeSnapshotV107,
    ExecutionGateSetV107,
    ExecutionGateV107,
    ExecutionJournalV107,
    ExecutionReceiptV107,
    ExecutionReplayLedgerV107,
    ExecutionStateV107,
    ReceiptStatusV107,
    ReplayErrorV107,
    SignatureErrorV107,
    SignedDeploymentExecutionCommandV107,
    StateTransitionErrorV107,
    ValidationErrorV107,
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


def test_digest_rejects_unknown_object():
    with pytest.raises(TypeError):
        digest_v107(object())


@pytest.mark.parametrize("changes", [
    {"min_replicas": 0},
    {"min_replicas": 3, "max_replicas": 2},
    {"rollback_replicas": 2},
    {"max_command_lifetime_seconds": 0},
    {"max_clock_skew_seconds": 0},
    {"max_readiness_wait_seconds": 0},
    {"recovery_claim_ttl_seconds": 0},
])
def test_policy_rejects_invalid_bounds(policy, changes):
    with pytest.raises(ValidationErrorV107):
        replace(policy, **changes)


@pytest.mark.parametrize("changes", [
    {"command_id": "bad id"},
    {"expected_resource_version": ""},
    {"expected_generation": 0},
    {"fencing_token": 0},
    {"expected_current_replicas": -1},
    {"target_replicas": -1},
    {"qualification_evidence_digest": "x"},
    {"expected_image_digest": "sha256:bad"},
    {"issued_at": datetime(2026, 1, 1)},
])
def test_intent_rejects_invalid_fields(intent, changes):
    with pytest.raises(ValidationErrorV107):
        replace(intent, **changes)


def test_intent_rejects_bad_interval_and_non_decreasing_rollback(intent):
    with pytest.raises(ValidationErrorV107, match="interval"):
        replace(intent, not_before=intent.expires_at)
    rollback = replace(intent, action=DeploymentActionV107.ROLLBACK, expected_current_replicas=4, target_replicas=1)
    with pytest.raises(ValidationErrorV107, match="reduce"):
        replace(rollback, target_replicas=4)


@pytest.mark.parametrize("changes,match", [
    ({"cluster": "other"}, "scope"),
    ({"expected_config_digest": "sha256:" + "9" * 64}, "release identity"),
    ({"expected_current_replicas": 11, "target_replicas": 12}, "current replica"),
    ({"target_replicas": 11}, "promotion target"),
    ({"expires_at": NOW + timedelta(minutes=20)}, "lifetime"),
])
def test_intent_policy_mismatch(intent, policy, changes, match):
    changed = replace(intent, **changes)
    with pytest.raises(ValidationErrorV107, match=match):
        changed.verify_policy(policy)


def test_weak_signing_secrets_are_rejected(intent, command):
    with pytest.raises(ValidationErrorV107, match="32 bytes"):
        ApprovalAttestationV107.sign(
            approval_id="a", intent=intent, approver_id="p", role=ApprovalRoleV107.RELEASE,
            signed_at=NOW, nonce="n", key_id="k", secret=b"weak",
        )
    with pytest.raises(ValidationErrorV107, match="32 bytes"):
        SignedDeploymentExecutionCommandV107.sign(
            intent=intent, approvals=command.approvals, controller_key_id="controller-key", controller_secret=b"weak",
        )


def test_approval_binding_validity_and_key_errors(intent, command):
    approval = command.approvals[0]
    with pytest.raises(SignatureErrorV107, match="bound"):
        replace(approval, intent_digest="0" * 64).verify(keyring={"release-key": RELEASE_SECRET}, intent=intent)
    with pytest.raises(SignatureErrorV107, match="validity"):
        replace(approval, signed_at=intent.expires_at).verify(keyring={"release-key": RELEASE_SECRET}, intent=intent)
    with pytest.raises(SignatureErrorV107, match="unknown"):
        approval.verify(keyring={}, intent=intent)
    with pytest.raises(ValidationErrorV107, match="32 bytes"):
        approval.verify(keyring={"release-key": b"weak"}, intent=intent)


def test_command_constructor_and_validity_edges(policy, intent, command):
    with pytest.raises(ValidationErrorV107, match="exactly two"):
        SignedDeploymentExecutionCommandV107(
            intent=intent, approvals=(command.approvals[0],), controller_key_id="controller-key", controller_signature="0" * 64,
        )
    with pytest.raises(SignatureErrorV107, match="not yet"):
        command.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={"controller-key": CONTROLLER_SECRET},
            now=NOW - timedelta(seconds=10),
        )
    with pytest.raises(SignatureErrorV107, match="unknown controller"):
        command.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={}, now=NOW,
        )
    with pytest.raises(ValidationErrorV107, match="validity enforcement"):
        command.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={"controller-key": CONTROLLER_SECRET}, now=NOW,
            replay_ledger=ExecutionReplayLedgerV107(), enforce_validity=False,
        )


def test_duplicate_approval_ids_and_nonces_are_rejected(policy, command):
    second = replace(
        command.approvals[1],
        approval_id=command.approvals[0].approval_id,
        nonce=command.approvals[0].nonce,
    )
    duplicate = replace(command, approvals=(command.approvals[0], second))
    with pytest.raises(SignatureErrorV107, match="unique"):
        duplicate.verify(
            policy=policy,
            approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
            controller_keyring={"controller-key": CONTROLLER_SECRET}, now=NOW,
        )


def test_replay_ledger_blocks_nonce_and_idempotency_reuse():
    ledger = ExecutionReplayLedgerV107()
    ledger.consume(command_id="c1", nonce="n1", idempotency_key="i1", observed_at=NOW)
    with pytest.raises(ReplayErrorV107, match="nonce"):
        ledger.consume(command_id="c2", nonce="n1", idempotency_key="i2", observed_at=NOW)
    with pytest.raises(ReplayErrorV107, match="idempotency"):
        ledger.consume(command_id="c3", nonce="n3", idempotency_key="i1", observed_at=NOW)


@pytest.mark.parametrize("changes", [
    {"generation": 0},
    {"replicas": -1},
    {"ready_replicas": 3},
    {"available_replicas": 3},
    {"fencing_token_annotation": 0, "action_id_annotation": "a", "command_digest_annotation": "a" * 64, "target_replicas_annotation": 1},
    {"target_replicas_annotation": -1, "action_id_annotation": "a", "command_digest_annotation": "a" * 64, "fencing_token_annotation": 1},
])
def test_snapshot_rejects_invalid_counts_or_markers(snapshot, changes):
    with pytest.raises(ValidationErrorV107):
        replace(snapshot, **changes)


def test_snapshot_rejects_marker_without_annotations(snapshot):
    with pytest.raises(ValidationErrorV107, match="without annotations"):
        replace(
            snapshot,
            metadata_annotations_present=False,
            action_id_annotation="a",
            command_digest_annotation="a" * 64,
            fencing_token_annotation=1,
            target_replicas_annotation=1,
        )


def test_gate_and_gate_set_validation():
    with pytest.raises(ValidationErrorV107, match="reason"):
        ExecutionGateV107("g", True, "", "a" * 64)
    with pytest.raises(ValidationErrorV107, match="empty"):
        ExecutionGateSetV107(())


def test_receipt_consistency_and_unknown_key(command, snapshot):
    with pytest.raises(ValidationErrorV107, match="patch digest"):
        ExecutionReceiptV107.sign(
            receipt_id="r", command=command, worker_id="w", status=ReceiptStatusV107.REJECTED,
            observed_at=NOW, pre_snapshot_digest=snapshot.snapshot_digest, post_snapshot_digest=None,
            patch_digest=None, mutation_attempted=True, reason="x", executor_key_id="k", executor_secret=EXECUTOR_SECRET,
        )
    with pytest.raises(ValidationErrorV107, match="cannot contain"):
        ExecutionReceiptV107.sign(
            receipt_id="r", command=command, worker_id="w", status=ReceiptStatusV107.REJECTED,
            observed_at=NOW, pre_snapshot_digest=snapshot.snapshot_digest, post_snapshot_digest=None,
            patch_digest="a" * 64, mutation_attempted=False, reason="x", executor_key_id="k", executor_secret=EXECUTOR_SECRET,
        )
    receipt = ExecutionReceiptV107.sign(
        receipt_id="r", command=command, worker_id="w", status=ReceiptStatusV107.REJECTED,
        observed_at=NOW, pre_snapshot_digest=snapshot.snapshot_digest, post_snapshot_digest=None,
        patch_digest=None, mutation_attempted=False, reason="x", executor_key_id="k", executor_secret=EXECUTOR_SECRET,
    )
    with pytest.raises(SignatureErrorV107, match="unknown"):
        receipt.verify({})


def test_journal_rejects_time_regression_and_detects_digest_tamper():
    journal = ExecutionJournalV107()
    journal.append("ONE", {}, NOW)
    with pytest.raises(ValidationErrorV107, match="monotonic"):
        journal.append("TWO", {}, NOW - timedelta(seconds=1))
    journal.append("TWO", {}, NOW + timedelta(seconds=1))
    journal._events[1] = replace(journal._events[1], event_digest="f" * 64)
    assert not journal.verify()
    assert len(journal.snapshot()) == 2


def test_coordinator_rejects_invalid_transitions(policy, command, snapshot):
    c = DeploymentExecutionCoordinatorV107(worker_id="w", policy=policy, command=command)
    with pytest.raises(StateTransitionErrorV107):
        c.record_preflight(ExecutionGateSetV107((ExecutionGateV107("g", True, "ok", "a" * 64),)), NOW)
    c.claim(NOW)
    with pytest.raises(StateTransitionErrorV107):
        c.claim(NOW)
    gates = evaluate_execution_preflight_v107(policy=policy, command=command, snapshot=snapshot)
    c.record_preflight(gates, NOW)
    with pytest.raises(StateTransitionErrorV107):
        c.mark_uncertain(NOW, "x")
    with pytest.raises(StateTransitionErrorV107):
        DeploymentExecutionCoordinatorV107(worker_id="x", policy=policy, command=command).start_verification(NOW)


def test_coordinator_blocks_mutation_for_already_applied(policy, command, snapshot):
    applied = replace(
        snapshot, resource_version="101", generation=8, replicas=4, ready_replicas=4, available_replicas=4,
        action_id_annotation=command.intent.action_id, command_digest_annotation=command.command_digest,
        fencing_token_annotation=command.intent.fencing_token, target_replicas_annotation=4,
    )
    c = DeploymentExecutionCoordinatorV107(worker_id="w", policy=policy, command=command)
    c.claim(NOW)
    c.record_preflight(evaluate_execution_preflight_v107(policy=policy, command=command, snapshot=applied), NOW)
    with pytest.raises(StateTransitionErrorV107, match="already-applied"):
        c.start_mutation("a" * 64, NOW)


def test_coordinator_rejects_unbound_receipt(policy, command, snapshot):
    c = DeploymentExecutionCoordinatorV107(worker_id="w", policy=policy, command=command)
    c.claim(NOW)
    c.record_preflight(evaluate_execution_preflight_v107(policy=policy, command=command, snapshot=snapshot), NOW)
    receipt = ExecutionReceiptV107.sign(
        receipt_id="r", command=command, worker_id="other", status=ReceiptStatusV107.REJECTED,
        observed_at=NOW, pre_snapshot_digest=snapshot.snapshot_digest, post_snapshot_digest=None,
        patch_digest=None, mutation_attempted=False, reason="x", executor_key_id="k", executor_secret=EXECUTOR_SECRET,
    )
    with pytest.raises(ValidationErrorV107, match="bound"):
        c.finish(receipt)


def test_state_digest_is_stable(policy, command):
    c = DeploymentExecutionCoordinatorV107(worker_id="w", policy=policy, command=command)
    assert c.state_digest == c.state_digest
