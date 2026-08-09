from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import pytest

from app.runtime.kubernetes_rollout_adapter_v107 import (
    KubernetesAmbiguousMutationV107,
    KubernetesDeploymentObservationV107,
    KubernetesMutationRejectedV107,
)
from app.runtime.postgres_rollout_repository_v107 import ClaimedExecutionV107
from app.runtime.rollout_execution_v107 import (
    ExecutionStateV107,
    ReceiptStatusV107,
    digest_v107,
)
from app.runtime.rollout_service_v107 import DeploymentRolloutServiceV107
from tests.conftest import (
    CONFIG,
    CONTROLLER_SECRET,
    EXECUTOR_SECRET,
    NOW,
    RELEASE_SECRET,
    RISK_SECRET,
)


class FakeClock:
    def __init__(self, current=NOW):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


class FakeRepository:
    def __init__(self, command=None):
        self.command = command
        self.state = ExecutionStateV107.PENDING
        self.claimed_by = None
        self.recovery_by = None
        self.mutation_attempts = 0
        self.patch_digest = None
        self.pre_snapshot_digest = None
        self.receipt = None
        self.calls = []
        self.enqueued = False

    def enqueue(self, command, observed_at):
        self.calls.append(("enqueue", observed_at))
        if self.enqueued:
            raise RuntimeError("duplicate")
        self.enqueued = True
        self.command = command

    def claim_next(self, *, worker_id, observed_at):
        self.calls.append(("claim_next", observed_at))
        if self.command is None or self.state != ExecutionStateV107.PENDING:
            return None
        self.state = ExecutionStateV107.CLAIMED
        self.claimed_by = worker_id
        return ClaimedExecutionV107(self.command, self.state, worker_id, self.mutation_attempts, self.patch_digest, self.pre_snapshot_digest)

    def record_preflight(self, **kwargs):
        self.calls.append(("record_preflight", kwargs))
        self.pre_snapshot_digest = kwargs["pre_snapshot_digest"]
        self.state = ExecutionStateV107.PREFLIGHT if kwargs["passed"] else ExecutionStateV107.QUARANTINED

    def mark_mutation_started(self, **kwargs):
        self.calls.append(("mark_mutation_started", kwargs))
        assert self.mutation_attempts == 0
        self.mutation_attempts = 1
        self.patch_digest = kwargs["patch_digest"]
        self.state = ExecutionStateV107.MUTATION_STARTED

    def mark_verifying(self, **kwargs):
        self.calls.append(("mark_verifying", kwargs))
        self.state = ExecutionStateV107.VERIFYING

    def mark_uncertain(self, **kwargs):
        self.calls.append(("mark_uncertain", kwargs))
        self.state = ExecutionStateV107.UNCERTAIN

    def complete(self, **kwargs):
        self.calls.append(("complete", kwargs))
        self.receipt = kwargs["receipt"]
        if self.receipt.status in {ReceiptStatusV107.APPLIED, ReceiptStatusV107.ALREADY_APPLIED, ReceiptStatusV107.RECONCILED}:
            self.state = ExecutionStateV107.SUCCEEDED
        elif self.receipt.status == ReceiptStatusV107.UNCERTAIN:
            self.state = ExecutionStateV107.UNCERTAIN
        else:
            self.state = ExecutionStateV107.FAILED

    def claim_recovery(self, *, command_id, worker_id, observed_at, claim_ttl_seconds):
        self.calls.append(("claim_recovery", command_id, worker_id, claim_ttl_seconds))
        if self.command is None or command_id != self.command.intent.command_id:
            raise RuntimeError("missing")
        self.recovery_by = worker_id
        return ClaimedExecutionV107(
            self.command,
            self.state,
            worker_id,
            self.mutation_attempts,
            self.patch_digest,
            self.pre_snapshot_digest,
        )


class FakeKubernetes:
    def __init__(self, observations, patch_result=None):
        self.observations = list(observations)
        self.patch_result = patch_result
        self.patch_calls = 0
        self.read_calls = 0

    def read_observation(self, *, namespace, deployment_name):
        self.read_calls += 1
        if not self.observations:
            raise RuntimeError("no observation")
        if len(self.observations) == 1:
            item = self.observations[0]
        else:
            item = self.observations.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def build_patch(self, *, command, snapshot, current_annotations):
        return b'[{"op":"replace","path":"/spec/replicas","value":4}]'

    def patch_digest(self, patch):
        return digest_v107(patch.decode())

    def apply_patch_once(self, *, command, patch):
        self.patch_calls += 1
        if isinstance(self.patch_result, Exception):
            raise self.patch_result
        return self.patch_result


def observation(snapshot, annotations=None):
    return KubernetesDeploymentObservationV107(
        snapshot=snapshot,
        annotations=annotations or {
            "astra.openai.com/config-digest": CONFIG,
            "astra.openai.com/external-order-routing-allowed": "false",
            "astra.openai.com/live-trading-allowed": "false",
        },
    )


def applied_snapshot(command, snapshot, *, ready=4, available=4):
    return replace(
        snapshot,
        resource_version="101",
        generation=8,
        replicas=4,
        ready_replicas=ready,
        available_replicas=available,
        action_id_annotation=command.intent.action_id,
        command_digest_annotation=command.command_digest,
        fencing_token_annotation=command.intent.fencing_token,
        target_replicas_annotation=command.intent.target_replicas,
    )


def service(policy, command, repo, kube, clock):
    return DeploymentRolloutServiceV107(
        worker_id="worker-1",
        policy=policy,
        repository=repo,
        kubernetes=kube,
        approval_keyring={"release-key": RELEASE_SECRET, "risk-key": RISK_SECRET},
        controller_keyring={"controller-key": CONTROLLER_SECRET},
        executor_key_id="executor-key",
        executor_secret=EXECUTOR_SECRET,
        clock=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        poll_interval_seconds=1,
    )


def test_enqueue_verifies_before_durable_write(policy, command):
    repo = FakeRepository()
    clock = FakeClock()
    svc = service(policy, command, repo, FakeKubernetes([]), clock)
    svc.enqueue(command)
    assert repo.enqueued
    bad = replace(command, controller_signature="0" * 64)
    with pytest.raises(Exception):
        svc.enqueue(bad)


def test_run_once_returns_none_without_work(policy, command):
    svc = service(policy, command, FakeRepository(), FakeKubernetes([]), FakeClock())
    assert svc.run_once() is None


def test_happy_path_mutates_once_and_verifies_all_replicas(policy, command, snapshot):
    repo = FakeRepository(command)
    post = applied_snapshot(command, snapshot)
    kube = FakeKubernetes([observation(snapshot), observation(post)], patch_result=post)
    clock = FakeClock()
    receipt = service(policy, command, repo, kube, clock).run_once()
    assert receipt.status == ReceiptStatusV107.APPLIED
    assert receipt.mutation_attempted
    assert kube.patch_calls == 1
    assert repo.mutation_attempts == 1
    assert repo.state == ExecutionStateV107.SUCCEEDED


def test_preflight_failure_never_mutates(policy, command, snapshot):
    repo = FakeRepository(command)
    bad = replace(snapshot, live_trading_allowed=True)
    kube = FakeKubernetes([observation(bad)])
    receipt = service(policy, command, repo, kube, FakeClock()).run_once()
    assert receipt.status == ReceiptStatusV107.REJECTED
    assert not receipt.mutation_attempted
    assert kube.patch_calls == 0


def test_already_applied_is_get_only(policy, command, snapshot):
    repo = FakeRepository(command)
    applied = applied_snapshot(command, snapshot)
    kube = FakeKubernetes([observation(applied)])
    receipt = service(policy, command, repo, kube, FakeClock()).run_once()
    assert receipt.status == ReceiptStatusV107.ALREADY_APPLIED
    assert not receipt.mutation_attempted
    assert kube.patch_calls == 0


def test_command_expiring_during_preflight_is_rejected_before_marker(policy, command, snapshot):
    repo = FakeRepository(command)
    clock = FakeClock(command.intent.expires_at - timedelta(seconds=4))

    class SlowKube(FakeKubernetes):
        def read_observation(self, **kwargs):
            result = super().read_observation(**kwargs)
            clock.advance(10)
            return result

    kube = SlowKube([observation(snapshot)])
    receipt = service(policy, command, repo, kube, clock).run_once()
    assert receipt.status == ReceiptStatusV107.REJECTED
    assert repo.mutation_attempts == 0
    assert kube.patch_calls == 0
    assert not any(call[0] == "mark_mutation_started" for call in repo.calls)


def test_ambiguous_patch_is_uncertain_and_never_retried(policy, command, snapshot):
    repo = FakeRepository(command)
    kube = FakeKubernetes(
        [observation(snapshot)],
        patch_result=KubernetesAmbiguousMutationV107("timeout"),
    )
    receipt = service(policy, command, repo, kube, FakeClock()).run_once()
    assert receipt.status == ReceiptStatusV107.UNCERTAIN
    assert receipt.mutation_attempted
    assert kube.patch_calls == 1
    assert repo.state == ExecutionStateV107.UNCERTAIN


def test_known_patch_rejection_is_failed_not_uncertain(policy, command, snapshot):
    repo = FakeRepository(command)
    kube = FakeKubernetes(
        [observation(snapshot)],
        patch_result=KubernetesMutationRejectedV107("forbidden"),
    )
    receipt = service(policy, command, repo, kube, FakeClock()).run_once()
    assert receipt.status == ReceiptStatusV107.REJECTED
    assert receipt.mutation_attempted
    assert repo.state == ExecutionStateV107.FAILED


def test_readiness_timeout_is_uncertain_without_second_patch(policy, command, snapshot):
    repo = FakeRepository(command)
    not_ready = applied_snapshot(command, snapshot, ready=2, available=2)
    kube = FakeKubernetes([observation(snapshot), observation(not_ready)], patch_result=not_ready)
    clock = FakeClock()
    receipt = service(policy, command, repo, kube, clock).run_once()
    assert receipt.status == ReceiptStatusV107.UNCERTAIN
    assert kube.patch_calls == 1
    assert clock.current >= NOW + timedelta(seconds=policy.max_readiness_wait_seconds)


def test_recovery_is_get_only_and_can_reconcile_after_ambiguous_patch(policy, command, snapshot):
    repo = FakeRepository(command)
    repo.state = ExecutionStateV107.UNCERTAIN
    repo.mutation_attempts = 1
    repo.patch_digest = "a" * 64
    repo.pre_snapshot_digest = snapshot.snapshot_digest
    applied = applied_snapshot(command, snapshot)
    kube = FakeKubernetes([observation(applied)])
    receipt = service(policy, command, repo, kube, FakeClock()).recover(command_id=command.intent.command_id)
    assert receipt.status == ReceiptStatusV107.RECONCILED
    assert receipt.mutation_attempted
    assert kube.patch_calls == 0
    assert repo.state == ExecutionStateV107.SUCCEEDED


def test_recovery_preserves_zero_mutation_attempts_for_already_applied_uncertainty(policy, command, snapshot):
    repo = FakeRepository(command)
    repo.state = ExecutionStateV107.UNCERTAIN
    repo.mutation_attempts = 0
    repo.pre_snapshot_digest = snapshot.snapshot_digest
    applied = applied_snapshot(command, snapshot)
    receipt = service(policy, command, repo, FakeKubernetes([observation(applied)]), FakeClock()).recover(
        command_id=command.intent.command_id
    )
    assert receipt.status == ReceiptStatusV107.RECONCILED
    assert not receipt.mutation_attempted
    assert repo.mutation_attempts == 0


def test_recovery_accepts_expired_command_but_still_verifies_signatures(policy, command, snapshot):
    repo = FakeRepository(command)
    repo.state = ExecutionStateV107.UNCERTAIN
    repo.mutation_attempts = 1
    repo.patch_digest = "a" * 64
    repo.pre_snapshot_digest = snapshot.snapshot_digest
    applied = applied_snapshot(command, snapshot)
    clock = FakeClock(command.intent.expires_at + timedelta(hours=1))
    receipt = service(policy, command, repo, FakeKubernetes([observation(applied)]), clock).recover(
        command_id=command.intent.command_id
    )
    assert receipt.status == ReceiptStatusV107.RECONCILED


def test_recovery_with_incomplete_rollout_remains_uncertain(policy, command, snapshot):
    repo = FakeRepository(command)
    repo.state = ExecutionStateV107.MUTATION_STARTED
    repo.mutation_attempts = 1
    repo.patch_digest = "a" * 64
    repo.pre_snapshot_digest = snapshot.snapshot_digest
    not_ready = applied_snapshot(command, snapshot, ready=3, available=3)
    clock = FakeClock()
    receipt = service(policy, command, repo, FakeKubernetes([observation(not_ready)]), clock).recover(
        command_id=command.intent.command_id
    )
    assert receipt.status == ReceiptStatusV107.UNCERTAIN
    assert repo.state == ExecutionStateV107.UNCERTAIN


def test_durable_fence_conflict_rejects_before_patch(policy, command, snapshot):
    from app.runtime.postgres_rollout_repository_v107 import PostgreSQLConflictV107

    class FenceRepo(FakeRepository):
        def mark_mutation_started(self, **kwargs):
            self.calls.append(("mark_mutation_started", kwargs))
            raise PostgreSQLConflictV107("stale fence")

    repo = FenceRepo(command)
    kube = FakeKubernetes([observation(snapshot)])
    receipt = service(policy, command, repo, kube, FakeClock()).run_once()
    assert receipt.status == ReceiptStatusV107.REJECTED
    assert not receipt.mutation_attempted
    assert kube.patch_calls == 0


def test_patch_construction_failure_rejects_before_marker(policy, command, snapshot):
    from app.runtime.rollout_execution_v107 import ValidationErrorV107

    class BadPatchKube(FakeKubernetes):
        def build_patch(self, **kwargs):
            raise ValidationErrorV107("annotations missing")

    repo = FakeRepository(command)
    kube = BadPatchKube([observation(snapshot)])
    receipt = service(policy, command, repo, kube, FakeClock()).run_once()
    assert receipt.status == ReceiptStatusV107.REJECTED
    assert not receipt.mutation_attempted
    assert repo.mutation_attempts == 0


def test_unexpected_exception_after_marker_becomes_uncertain(policy, command, snapshot):
    repo = FakeRepository(command)
    kube = FakeKubernetes([observation(snapshot)], patch_result=RuntimeError("client bug"))
    receipt = service(policy, command, repo, kube, FakeClock()).run_once()
    assert receipt.status == ReceiptStatusV107.UNCERTAIN
    assert receipt.mutation_attempted
    assert repo.mutation_attempts == 1
    assert kube.patch_calls == 1


def test_verification_transport_error_can_recover_before_deadline(policy, command, snapshot):
    from app.runtime.kubernetes_rollout_adapter_v107 import KubernetesTransportErrorV107

    repo = FakeRepository(command)
    post = applied_snapshot(command, snapshot)
    kube = FakeKubernetes(
        [observation(snapshot), KubernetesTransportErrorV107("temporary"), observation(post)],
        patch_result=post,
    )
    clock = FakeClock()
    receipt = service(policy, command, repo, kube, clock).run_once()
    assert receipt.status == ReceiptStatusV107.APPLIED
    assert kube.patch_calls == 1
    assert kube.read_calls == 3


def test_recovery_uses_explicit_recovery_worker_identity(policy, command, snapshot):
    repo = FakeRepository(command)
    repo.state = ExecutionStateV107.UNCERTAIN
    repo.mutation_attempts = 1
    repo.patch_digest = "a" * 64
    repo.pre_snapshot_digest = snapshot.snapshot_digest
    applied = applied_snapshot(command, snapshot)
    receipt = service(policy, command, repo, FakeKubernetes([observation(applied)]), FakeClock()).recover(
        command_id=command.intent.command_id,
        recovery_worker_id="recovery-2",
    )
    assert receipt.worker_id == "recovery-2"
    assert repo.recovery_by == "recovery-2"
