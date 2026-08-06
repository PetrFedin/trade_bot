from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time
from typing import Callable, Mapping

from app.runtime.kubernetes_rollout_adapter_v107 import (
    KubernetesAmbiguousMutationV107,
    KubernetesDeploymentObservationV107,
    KubernetesMutationRejectedV107,
    KubernetesPreconditionFailedV107,
    KubernetesResponseErrorV107,
    KubernetesRolloutAdapterV107,
    KubernetesTransportErrorV107,
)
from app.runtime.postgres_rollout_repository_v107 import (
    ClaimedExecutionV107,
    PostgreSQLConflictV107,
    PostgreSQLRolloutRepositoryV107,
)
from app.runtime.rollout_execution_v107 import (
    DeploymentExecutionCoordinatorV107,
    DeploymentExecutionPolicyV107,
    ExecutionGateSetV107,
    ExecutionGateV107,
    ExecutionReceiptV107,
    ExecutionStateV107,
    ReceiptStatusV107,
    SignatureErrorV107,
    SignedDeploymentExecutionCommandV107,
    ValidationErrorV107,
    certify_full_rollout_v107,
    digest_v107,
    evaluate_execution_preflight_v107,
)

UTC = timezone.utc
ClockV107 = Callable[[], datetime]
SleeperV107 = Callable[[float], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationErrorV107("datetime must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(slots=True)
class DeploymentRolloutServiceV107:
    worker_id: str
    policy: DeploymentExecutionPolicyV107
    repository: PostgreSQLRolloutRepositoryV107
    kubernetes: KubernetesRolloutAdapterV107
    approval_keyring: Mapping[str, bytes]
    controller_keyring: Mapping[str, bytes]
    executor_key_id: str
    executor_secret: bytes
    clock: ClockV107 = _utc_now
    sleeper: SleeperV107 = time.sleep
    poll_interval_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.worker_id or not self.executor_key_id:
            raise ValidationErrorV107("worker_id and executor_key_id are required")
        if len(self.executor_secret) < 32:
            raise ValidationErrorV107("executor_secret must be at least 32 bytes")
        if self.poll_interval_seconds <= 0:
            raise ValidationErrorV107("poll_interval_seconds must be positive")

    def enqueue(self, command: SignedDeploymentExecutionCommandV107, observed_at: datetime | None = None) -> None:
        current = _ensure_utc(observed_at or self.clock())
        command.verify(
            policy=self.policy,
            approval_keyring=self.approval_keyring,
            controller_keyring=self.controller_keyring,
            now=current,
            enforce_validity=True,
        )
        self.repository.enqueue(command, current)

    def run_once(self, observed_at: datetime | None = None) -> ExecutionReceiptV107 | None:
        current = _ensure_utc(observed_at or self.clock())
        claimed = self.repository.claim_next(worker_id=self.worker_id, observed_at=current)
        if claimed is None:
            return None
        return self.execute_claimed(claimed)

    def execute_claimed(self, claimed: ClaimedExecutionV107) -> ExecutionReceiptV107:
        command = claimed.command
        coordinator = DeploymentExecutionCoordinatorV107(
            worker_id=self.worker_id,
            policy=self.policy,
            command=command,
        )
        now = _ensure_utc(self.clock())
        coordinator.claim(now)
        try:
            command.verify(
                policy=self.policy,
                approval_keyring=self.approval_keyring,
                controller_keyring=self.controller_keyring,
                now=now,
                enforce_validity=True,
            )
            observation = self.kubernetes.read_observation(
                namespace=command.intent.namespace,
                deployment_name=command.intent.deployment_name,
            )
        except (SignatureErrorV107, ValidationErrorV107, KubernetesTransportErrorV107, KubernetesResponseErrorV107) as exc:
            return self._reject_before_mutation(coordinator, command, str(exc))

        preflight = evaluate_execution_preflight_v107(
            policy=self.policy,
            command=command,
            snapshot=observation.snapshot,
        )
        now = _ensure_utc(self.clock())
        coordinator.record_preflight(preflight, now)
        self.repository.record_preflight(
            command_id=command.intent.command_id,
            worker_id=self.worker_id,
            passed=preflight.passed,
            gates_digest=preflight.digest,
            pre_snapshot_digest=observation.snapshot.snapshot_digest,
            observed_at=now,
        )
        if not preflight.passed:
            receipt = self._receipt(
                command=command,
                status=ReceiptStatusV107.REJECTED,
                pre_snapshot_digest=observation.snapshot.snapshot_digest,
                post_snapshot_digest=None,
                patch_digest=None,
                mutation_attempted=False,
                reason="execution preflight failed",
            )
            coordinator.finish(receipt)
            self.repository.complete(
                command_id=command.intent.command_id,
                worker_id=self.worker_id,
                receipt=receipt,
                observed_at=receipt.observed_at,
            )
            return receipt

        if preflight.already_applied:
            coordinator.start_verification(_ensure_utc(self.clock()))
            self.repository.mark_verifying(
                command_id=command.intent.command_id,
                worker_id=self.worker_id,
                observed_at=_ensure_utc(self.clock()),
            )
            return self._verify_and_finish(
                coordinator=coordinator,
                command=command,
                pre_snapshot_digest=observation.snapshot.snapshot_digest,
                patch_digest=None,
                mutation_attempted=False,
                success_status=ReceiptStatusV107.ALREADY_APPLIED,
            )

        try:
            patch = self.kubernetes.build_patch(
                command=command,
                snapshot=observation.snapshot,
                current_annotations=observation.annotations,
            )
            patch_digest = self.kubernetes.patch_digest(patch)
        except ValidationErrorV107 as exc:
            receipt = self._receipt(
                command=command,
                status=ReceiptStatusV107.REJECTED,
                pre_snapshot_digest=observation.snapshot.snapshot_digest,
                post_snapshot_digest=None,
                patch_digest=None,
                mutation_attempted=False,
                reason=f"safe patch construction failed: {exc}",
            )
            coordinator.finish(receipt)
            self.repository.complete(
                command_id=command.intent.command_id,
                worker_id=self.worker_id,
                receipt=receipt,
                observed_at=receipt.observed_at,
            )
            return receipt

        # Re-verify immediately before the durable mutation marker. A command may
        # expire while the preflight GET is in flight.
        now = _ensure_utc(self.clock())
        try:
            command.verify(
                policy=self.policy,
                approval_keyring=self.approval_keyring,
                controller_keyring=self.controller_keyring,
                now=now,
                enforce_validity=True,
            )
        except (SignatureErrorV107, ValidationErrorV107) as exc:
            receipt = self._receipt(
                command=command,
                status=ReceiptStatusV107.REJECTED,
                pre_snapshot_digest=observation.snapshot.snapshot_digest,
                post_snapshot_digest=None,
                patch_digest=None,
                mutation_attempted=False,
                reason=f"command invalid before mutation: {exc}",
            )
            coordinator.finish(receipt)
            self.repository.complete(
                command_id=command.intent.command_id,
                worker_id=self.worker_id,
                receipt=receipt,
                observed_at=receipt.observed_at,
            )
            return receipt

        try:
            self.repository.mark_mutation_started(
                command_id=command.intent.command_id,
                worker_id=self.worker_id,
                deployment_uid=command.intent.deployment_uid,
                fencing_token=command.intent.fencing_token,
                patch_digest=patch_digest,
                observed_at=now,
            )
        except PostgreSQLConflictV107 as exc:
            receipt = self._receipt(
                command=command,
                status=ReceiptStatusV107.REJECTED,
                pre_snapshot_digest=observation.snapshot.snapshot_digest,
                post_snapshot_digest=None,
                patch_digest=None,
                mutation_attempted=False,
                reason=f"durable mutation fence rejected: {exc}",
            )
            coordinator.finish(receipt)
            self.repository.complete(
                command_id=command.intent.command_id,
                worker_id=self.worker_id,
                receipt=receipt,
                observed_at=receipt.observed_at,
            )
            return receipt
        coordinator.start_mutation(patch_digest, now)

        try:
            self.kubernetes.apply_patch_once(command=command, patch=patch)
        except (KubernetesPreconditionFailedV107, KubernetesMutationRejectedV107) as exc:
            coordinator.start_verification(_ensure_utc(self.clock()))
            self.repository.mark_verifying(
                command_id=command.intent.command_id,
                worker_id=self.worker_id,
                observed_at=_ensure_utc(self.clock()),
            )
            receipt = self._receipt(
                command=command,
                status=ReceiptStatusV107.REJECTED,
                pre_snapshot_digest=observation.snapshot.snapshot_digest,
                post_snapshot_digest=None,
                patch_digest=patch_digest,
                mutation_attempted=True,
                reason=str(exc),
            )
            coordinator.finish(receipt)
            self.repository.complete(
                command_id=command.intent.command_id,
                worker_id=self.worker_id,
                receipt=receipt,
                observed_at=receipt.observed_at,
            )
            return receipt
        except KubernetesAmbiguousMutationV107 as exc:
            return self._finish_ambiguous_after_marker(
                coordinator=coordinator,
                command=command,
                pre_snapshot_digest=observation.snapshot.snapshot_digest,
                patch_digest=patch_digest,
                reason=str(exc),
            )
        except Exception as exc:
            # Any unexpected failure after the durable marker is treated as an
            # ambiguous side effect. Recovery remains GET-only and no second
            # PATCH is permitted.
            return self._finish_ambiguous_after_marker(
                coordinator=coordinator,
                command=command,
                pre_snapshot_digest=observation.snapshot.snapshot_digest,
                patch_digest=patch_digest,
                reason=f"unexpected post-marker failure: {type(exc).__name__}",
            )

        coordinator.start_verification(_ensure_utc(self.clock()))
        self.repository.mark_verifying(
            command_id=command.intent.command_id,
            worker_id=self.worker_id,
            observed_at=_ensure_utc(self.clock()),
        )
        return self._verify_and_finish(
            coordinator=coordinator,
            command=command,
            pre_snapshot_digest=observation.snapshot.snapshot_digest,
            patch_digest=patch_digest,
            mutation_attempted=True,
            success_status=ReceiptStatusV107.APPLIED,
        )

    def recover(
        self,
        *,
        command_id: str,
        recovery_worker_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> ExecutionReceiptV107:
        worker = recovery_worker_id or self.worker_id
        current = _ensure_utc(observed_at or self.clock())
        claimed = self.repository.claim_recovery(
            command_id=command_id,
            worker_id=worker,
            observed_at=current,
            claim_ttl_seconds=self.policy.recovery_claim_ttl_seconds,
        )
        command = claimed.command
        command.verify(
            policy=self.policy,
            approval_keyring=self.approval_keyring,
            controller_keyring=self.controller_keyring,
            now=current,
            enforce_validity=False,
        )
        coordinator = DeploymentExecutionCoordinatorV107(
            worker_id=worker,
            policy=self.policy,
            command=command,
            state=ExecutionStateV107.UNCERTAIN,
            mutation_attempts=claimed.mutation_attempts,
        )
        coordinator.start_verification(current)
        self.repository.mark_verifying(command_id=command_id, worker_id=worker, observed_at=current)
        return self._verify_and_finish(
            coordinator=coordinator,
            command=command,
            pre_snapshot_digest=claimed.pre_snapshot_digest or digest_v107({"command": command.command_digest, "missing": "pre_snapshot"}),
            patch_digest=claimed.patch_digest,
            mutation_attempted=claimed.mutation_attempts == 1,
            success_status=ReceiptStatusV107.RECONCILED,
            worker_id=worker,
        )

    def _reject_before_mutation(
        self,
        coordinator: DeploymentExecutionCoordinatorV107,
        command: SignedDeploymentExecutionCommandV107,
        reason: str,
    ) -> ExecutionReceiptV107:
        evidence = digest_v107({"command": command.command_digest, "failure": reason})
        gates = ExecutionGateSetV107((ExecutionGateV107("command_or_read", False, reason[:512] or "preflight failure", evidence),))
        now = _ensure_utc(self.clock())
        coordinator.record_preflight(gates, now)
        self.repository.record_preflight(
            command_id=command.intent.command_id,
            worker_id=self.worker_id,
            passed=False,
            gates_digest=gates.digest,
            pre_snapshot_digest=evidence,
            observed_at=now,
        )
        receipt = self._receipt(
            command=command,
            status=ReceiptStatusV107.REJECTED,
            pre_snapshot_digest=evidence,
            post_snapshot_digest=None,
            patch_digest=None,
            mutation_attempted=False,
            reason=reason[:512] or "preflight failure",
        )
        coordinator.finish(receipt)
        self.repository.complete(
            command_id=command.intent.command_id,
            worker_id=self.worker_id,
            receipt=receipt,
            observed_at=receipt.observed_at,
        )
        return receipt

    def _finish_ambiguous_after_marker(
        self,
        *,
        coordinator: DeploymentExecutionCoordinatorV107,
        command: SignedDeploymentExecutionCommandV107,
        pre_snapshot_digest: str,
        patch_digest: str,
        reason: str,
    ) -> ExecutionReceiptV107:
        now = _ensure_utc(self.clock())
        coordinator.mark_uncertain(now, reason)
        self.repository.mark_uncertain(
            command_id=command.intent.command_id,
            worker_id=self.worker_id,
            reason=reason[:512] or "ambiguous mutation",
            observed_at=now,
        )
        receipt = self._receipt(
            command=command,
            status=ReceiptStatusV107.UNCERTAIN,
            pre_snapshot_digest=pre_snapshot_digest,
            post_snapshot_digest=None,
            patch_digest=patch_digest,
            mutation_attempted=True,
            reason="PATCH outcome is ambiguous; recovery is GET-only",
        )
        coordinator.finish(receipt)
        self.repository.complete(
            command_id=command.intent.command_id,
            worker_id=self.worker_id,
            receipt=receipt,
            observed_at=receipt.observed_at,
        )
        return receipt

    def _wait_for_rollout(
        self,
        *,
        command: SignedDeploymentExecutionCommandV107,
    ) -> tuple[KubernetesDeploymentObservationV107 | None, ExecutionGateSetV107 | None, str]:
        deadline = _ensure_utc(self.clock()) + timedelta(seconds=self.policy.max_readiness_wait_seconds)
        last_observation: KubernetesDeploymentObservationV107 | None = None
        last_gates: ExecutionGateSetV107 | None = None
        while True:
            try:
                last_observation = self.kubernetes.read_observation(
                    namespace=command.intent.namespace,
                    deployment_name=command.intent.deployment_name,
                )
                last_gates = certify_full_rollout_v107(
                    policy=self.policy,
                    command=command,
                    snapshot=last_observation.snapshot,
                )
                if last_gates.passed:
                    return last_observation, last_gates, "full rollout verified"
            except (KubernetesTransportErrorV107, KubernetesResponseErrorV107) as exc:
                if _ensure_utc(self.clock()) >= deadline:
                    return last_observation, last_gates, f"readiness verification failed: {exc}"
            now = _ensure_utc(self.clock())
            if now >= deadline:
                return last_observation, last_gates, "readiness deadline exceeded"
            self.sleeper(min(self.poll_interval_seconds, max(0.0, (deadline - now).total_seconds())))

    def _verify_and_finish(
        self,
        *,
        coordinator: DeploymentExecutionCoordinatorV107,
        command: SignedDeploymentExecutionCommandV107,
        pre_snapshot_digest: str,
        patch_digest: str | None,
        mutation_attempted: bool,
        success_status: ReceiptStatusV107,
        worker_id: str | None = None,
    ) -> ExecutionReceiptV107:
        worker = worker_id or self.worker_id
        observation, gates, reason = self._wait_for_rollout(command=command)
        if observation is not None and gates is not None and gates.passed:
            receipt = self._receipt(
                command=command,
                status=success_status,
                pre_snapshot_digest=pre_snapshot_digest,
                post_snapshot_digest=observation.snapshot.snapshot_digest,
                patch_digest=patch_digest,
                mutation_attempted=mutation_attempted,
                reason=reason,
                worker_id=worker,
            )
        else:
            now = _ensure_utc(self.clock())
            coordinator.mark_uncertain(now, reason)
            self.repository.mark_uncertain(
                command_id=command.intent.command_id,
                worker_id=worker,
                reason=reason,
                observed_at=now,
            )
            receipt = self._receipt(
                command=command,
                status=ReceiptStatusV107.UNCERTAIN,
                pre_snapshot_digest=pre_snapshot_digest,
                post_snapshot_digest=observation.snapshot.snapshot_digest if observation is not None else None,
                patch_digest=patch_digest,
                mutation_attempted=mutation_attempted,
                reason=reason,
                worker_id=worker,
            )
        coordinator.finish(receipt)
        self.repository.complete(
            command_id=command.intent.command_id,
            worker_id=worker,
            receipt=receipt,
            observed_at=receipt.observed_at,
        )
        return receipt

    def _receipt(
        self,
        *,
        command: SignedDeploymentExecutionCommandV107,
        status: ReceiptStatusV107,
        pre_snapshot_digest: str,
        post_snapshot_digest: str | None,
        patch_digest: str | None,
        mutation_attempted: bool,
        reason: str,
        worker_id: str | None = None,
    ) -> ExecutionReceiptV107:
        worker = worker_id or self.worker_id
        return ExecutionReceiptV107.sign(
            receipt_id=f"receipt:{command.intent.command_id}:{status.value.lower()}",
            command=command,
            worker_id=worker,
            status=status,
            observed_at=_ensure_utc(self.clock()),
            pre_snapshot_digest=pre_snapshot_digest,
            post_snapshot_digest=post_snapshot_digest,
            patch_digest=patch_digest,
            mutation_attempted=mutation_attempted,
            reason=reason[:512] or "unspecified",
            executor_key_id=self.executor_key_id,
            executor_secret=self.executor_secret,
        )


EXTERNAL_ORDER_ROUTING_ALLOWED_V107 = False
LIVE_TRADING_ALLOWED_V107 = False
KUBERNETES_PATCH_RETRY_ALLOWED_V107 = False
RECOVERY_MUTATION_ALLOWED_V107 = False
