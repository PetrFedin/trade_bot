from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import json
import re
import threading
from typing import Any, Mapping, Sequence

UTC = timezone.utc
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RESOURCE_VERSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class RolloutExecutionErrorV107(RuntimeError):
    pass


class ValidationErrorV107(RolloutExecutionErrorV107):
    pass


class SignatureErrorV107(RolloutExecutionErrorV107):
    pass


class ReplayErrorV107(RolloutExecutionErrorV107):
    pass


class StateTransitionErrorV107(RolloutExecutionErrorV107):
    pass


class DeploymentActionV107(str, Enum):
    PROMOTE = "PROMOTE"
    ROLLBACK = "ROLLBACK"


class ApprovalRoleV107(str, Enum):
    RELEASE = "RELEASE"
    RISK = "RISK"


class ExecutionStateV107(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    PREFLIGHT = "PREFLIGHT"
    MUTATION_STARTED = "MUTATION_STARTED"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    QUARANTINED = "QUARANTINED"


class ReceiptStatusV107(str, Enum):
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    RECONCILED = "RECONCILED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationErrorV107("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _canonical(value: Any) -> bytes:
    def default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return _ensure_utc(obj).isoformat().replace("+00:00", "Z")
        if isinstance(obj, Enum):
            return obj.value
        if hasattr(obj, "to_payload"):
            return obj.to_payload()
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        raise TypeError(type(obj).__name__)

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=default).encode("utf-8")


def digest_v107(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValidationErrorV107(f"invalid {name}")


def _validate_hex(value: str, name: str) -> None:
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise ValidationErrorV107(f"invalid {name}")


def _validate_sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValidationErrorV107(f"invalid {name}")


def _validate_resource_version(value: str) -> None:
    if not isinstance(value, str) or not _RESOURCE_VERSION_RE.fullmatch(value):
        raise ValidationErrorV107("invalid resource_version")


def _require_secret(secret: bytes, name: str) -> None:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValidationErrorV107(f"{name} must be at least 32 bytes")


@dataclass(frozen=True, slots=True)
class DeploymentExecutionPolicyV107:
    cluster: str
    namespace: str
    deployment_name: str
    deployment_uid: str
    service_account: str
    expected_image_digest: str
    expected_config_digest: str
    min_replicas: int
    max_replicas: int
    rollback_replicas: int
    max_command_lifetime_seconds: int = 600
    max_clock_skew_seconds: int = 5
    max_readiness_wait_seconds: int = 600
    recovery_claim_ttl_seconds: int = 120
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    def __post_init__(self) -> None:
        for name in ("cluster", "namespace", "deployment_name", "deployment_uid", "service_account"):
            _validate_id(getattr(self, name), name)
        _validate_sha256(self.expected_image_digest, "expected_image_digest")
        _validate_sha256(self.expected_config_digest, "expected_config_digest")
        if self.min_replicas < 1 or self.max_replicas < self.min_replicas:
            raise ValidationErrorV107("invalid replica bounds")
        if not (0 <= self.rollback_replicas < self.min_replicas):
            raise ValidationErrorV107("rollback_replicas must be below min_replicas")
        for name in (
            "max_command_lifetime_seconds",
            "max_clock_skew_seconds",
            "max_readiness_wait_seconds",
            "recovery_claim_ttl_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValidationErrorV107(f"{name} must be positive")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise ValidationErrorV107("rollout execution cannot enable external routing or live trading")

    @property
    def policy_digest(self) -> str:
        return digest_v107(asdict(self))


@dataclass(frozen=True, slots=True)
class DeploymentExecutionIntentV107:
    command_id: str
    action_id: str
    qualification_id: str
    qualification_action_digest: str
    action: DeploymentActionV107
    cluster: str
    namespace: str
    deployment_name: str
    deployment_uid: str
    service_account: str
    expected_resource_version: str
    expected_generation: int
    expected_current_replicas: int
    target_replicas: int
    expected_image_digest: str
    expected_config_digest: str
    qualification_evidence_digest: str
    qualification_state_digest: str
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    idempotency_key: str
    fencing_token: int
    nonce: str

    def __post_init__(self) -> None:
        for name in (
            "command_id",
            "action_id",
            "qualification_id",
            "cluster",
            "namespace",
            "deployment_name",
            "deployment_uid",
            "service_account",
            "idempotency_key",
            "nonce",
        ):
            _validate_id(getattr(self, name), name)
        _validate_hex(self.qualification_action_digest, "qualification_action_digest")
        _validate_resource_version(self.expected_resource_version)
        _validate_sha256(self.expected_image_digest, "expected_image_digest")
        _validate_sha256(self.expected_config_digest, "expected_config_digest")
        _validate_hex(self.qualification_evidence_digest, "qualification_evidence_digest")
        _validate_hex(self.qualification_state_digest, "qualification_state_digest")
        if self.expected_generation <= 0 or self.fencing_token <= 0:
            raise ValidationErrorV107("generation and fencing_token must be positive")
        if self.expected_current_replicas < 0 or self.target_replicas < 0:
            raise ValidationErrorV107("replica counts must be non-negative")
        issued = _ensure_utc(self.issued_at)
        not_before = _ensure_utc(self.not_before)
        expires = _ensure_utc(self.expires_at)
        if not (issued <= not_before < expires):
            raise ValidationErrorV107("invalid command validity interval")
        if self.action == DeploymentActionV107.PROMOTE and self.target_replicas <= self.expected_current_replicas:
            raise ValidationErrorV107("promotion must increase replicas")
        if self.action == DeploymentActionV107.ROLLBACK and self.target_replicas >= self.expected_current_replicas:
            raise ValidationErrorV107("rollback must reduce replicas")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def intent_digest(self) -> str:
        return digest_v107(self.to_payload())

    def verify_policy(self, policy: DeploymentExecutionPolicyV107) -> None:
        if (
            self.cluster != policy.cluster
            or self.namespace != policy.namespace
            or self.deployment_name != policy.deployment_name
            or self.deployment_uid != policy.deployment_uid
            or self.service_account != policy.service_account
        ):
            raise ValidationErrorV107("command scope does not match execution policy")
        if (
            self.expected_image_digest != policy.expected_image_digest
            or self.expected_config_digest != policy.expected_config_digest
        ):
            raise ValidationErrorV107("command release identity does not match policy")
        if self.expected_current_replicas > policy.max_replicas:
            raise ValidationErrorV107("current replica count exceeds policy maximum")
        if self.action == DeploymentActionV107.PROMOTE:
            if not (policy.min_replicas <= self.target_replicas <= policy.max_replicas):
                raise ValidationErrorV107("promotion target outside policy")
        elif self.target_replicas != policy.rollback_replicas:
            raise ValidationErrorV107("rollback target does not match policy")
        lifetime = (_ensure_utc(self.expires_at) - _ensure_utc(self.issued_at)).total_seconds()
        if lifetime > policy.max_command_lifetime_seconds:
            raise ValidationErrorV107("command lifetime exceeds policy")


@dataclass(frozen=True, slots=True)
class ApprovalAttestationV107:
    approval_id: str
    intent_digest: str
    approver_id: str
    role: ApprovalRoleV107
    signed_at: datetime
    nonce: str
    key_id: str
    signature: str

    def __post_init__(self) -> None:
        for name in ("approval_id", "approver_id", "nonce", "key_id"):
            _validate_id(getattr(self, name), name)
        _validate_hex(self.intent_digest, "intent_digest")
        _validate_hex(self.signature, "signature")
        _ensure_utc(self.signed_at)

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature")
        return payload

    @classmethod
    def sign(
        cls,
        *,
        approval_id: str,
        intent: DeploymentExecutionIntentV107,
        approver_id: str,
        role: ApprovalRoleV107,
        signed_at: datetime,
        nonce: str,
        key_id: str,
        secret: bytes,
    ) -> "ApprovalAttestationV107":
        _require_secret(secret, "approval signing secret")
        unsigned = cls(
            approval_id=approval_id,
            intent_digest=intent.intent_digest,
            approver_id=approver_id,
            role=role,
            signed_at=signed_at,
            nonce=nonce,
            key_id=key_id,
            signature="0" * 64,
        )
        signature = hmac.new(secret, _canonical(unsigned.to_payload()), hashlib.sha256).hexdigest()
        return replace(unsigned, signature=signature)

    def verify(self, *, keyring: Mapping[str, bytes], intent: DeploymentExecutionIntentV107) -> None:
        if self.intent_digest != intent.intent_digest:
            raise SignatureErrorV107("approval is not bound to command intent")
        if not (_ensure_utc(intent.issued_at) <= _ensure_utc(self.signed_at) < _ensure_utc(intent.expires_at)):
            raise SignatureErrorV107("approval outside command validity interval")
        secret = keyring.get(self.key_id)
        if secret is None:
            raise SignatureErrorV107("unknown approval key")
        _require_secret(secret, "approval key")
        expected = hmac.new(secret, _canonical(self.to_payload()), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise SignatureErrorV107("approval signature mismatch")


@dataclass(frozen=True, slots=True)
class SignedDeploymentExecutionCommandV107:
    intent: DeploymentExecutionIntentV107
    approvals: tuple[ApprovalAttestationV107, ...]
    controller_key_id: str
    controller_signature: str

    def __post_init__(self) -> None:
        _validate_id(self.controller_key_id, "controller_key_id")
        _validate_hex(self.controller_signature, "controller_signature")
        if len(self.approvals) != 2:
            raise ValidationErrorV107("exactly two approvals are required")

    def to_payload(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_payload(),
            "approvals": [asdict(approval) for approval in self.approvals],
            "controller_key_id": self.controller_key_id,
        }

    @property
    def command_digest(self) -> str:
        return digest_v107(self.to_payload())

    @classmethod
    def sign(
        cls,
        *,
        intent: DeploymentExecutionIntentV107,
        approvals: Sequence[ApprovalAttestationV107],
        controller_key_id: str,
        controller_secret: bytes,
    ) -> "SignedDeploymentExecutionCommandV107":
        _require_secret(controller_secret, "controller signing secret")
        unsigned = cls(
            intent=intent,
            approvals=tuple(approvals),
            controller_key_id=controller_key_id,
            controller_signature="0" * 64,
        )
        signature = hmac.new(controller_secret, _canonical(unsigned.to_payload()), hashlib.sha256).hexdigest()
        return replace(unsigned, controller_signature=signature)

    def verify(
        self,
        *,
        policy: DeploymentExecutionPolicyV107,
        approval_keyring: Mapping[str, bytes],
        controller_keyring: Mapping[str, bytes],
        now: datetime,
        replay_ledger: "ExecutionReplayLedgerV107 | None" = None,
        enforce_validity: bool = True,
    ) -> None:
        current = _ensure_utc(now)
        self.intent.verify_policy(policy)
        if enforce_validity:
            skew = timedelta(seconds=policy.max_clock_skew_seconds)
            if current + skew < _ensure_utc(self.intent.not_before):
                raise SignatureErrorV107("command is not yet valid")
            if current - skew >= _ensure_utc(self.intent.expires_at):
                raise SignatureErrorV107("command has expired")

        approver_ids = {approval.approver_id for approval in self.approvals}
        key_ids = {approval.key_id for approval in self.approvals}
        approval_ids = {approval.approval_id for approval in self.approvals}
        nonces = {approval.nonce for approval in self.approvals}
        roles = {approval.role for approval in self.approvals}
        if len(approver_ids) != 2 or len(key_ids) != 2:
            raise SignatureErrorV107("approvals require distinct people and keys")
        if len(approval_ids) != 2 or len(nonces) != 2:
            raise SignatureErrorV107("approval ids and nonces must be unique")
        if roles != {ApprovalRoleV107.RELEASE, ApprovalRoleV107.RISK}:
            raise SignatureErrorV107("release and risk approvals are both required")
        for approval in self.approvals:
            approval.verify(keyring=approval_keyring, intent=self.intent)
            if enforce_validity and _ensure_utc(approval.signed_at) > current + timedelta(seconds=policy.max_clock_skew_seconds):
                raise SignatureErrorV107("approval signature is from the future")

        secret = controller_keyring.get(self.controller_key_id)
        if secret is None:
            raise SignatureErrorV107("unknown controller key")
        _require_secret(secret, "controller key")
        expected = hmac.new(secret, _canonical(self.to_payload()), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.controller_signature):
            raise SignatureErrorV107("controller signature mismatch")

        if replay_ledger is not None:
            if not enforce_validity:
                raise ValidationErrorV107("replay consumption requires validity enforcement")
            replay_ledger.consume(
                command_id=self.intent.command_id,
                nonce=self.intent.nonce,
                idempotency_key=self.intent.idempotency_key,
                observed_at=current,
            )


class ExecutionReplayLedgerV107:
    def __init__(self) -> None:
        self._command_ids: set[str] = set()
        self._nonces: set[str] = set()
        self._idempotency_keys: set[str] = set()
        self._lock = threading.Lock()

    def consume(self, *, command_id: str, nonce: str, idempotency_key: str, observed_at: datetime) -> None:
        _validate_id(command_id, "command_id")
        _validate_id(nonce, "nonce")
        _validate_id(idempotency_key, "idempotency_key")
        _ensure_utc(observed_at)
        with self._lock:
            if command_id in self._command_ids:
                raise ReplayErrorV107("command already consumed")
            if nonce in self._nonces:
                raise ReplayErrorV107("command nonce already consumed")
            if idempotency_key in self._idempotency_keys:
                raise ReplayErrorV107("idempotency key already consumed")
            self._command_ids.add(command_id)
            self._nonces.add(nonce)
            self._idempotency_keys.add(idempotency_key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._command_ids)


@dataclass(frozen=True, slots=True)
class DeploymentRuntimeSnapshotV107:
    cluster: str
    namespace: str
    deployment_name: str
    deployment_uid: str
    service_account: str
    resource_version: str
    generation: int
    replicas: int
    ready_replicas: int
    available_replicas: int
    image_digest: str
    config_digest: str
    external_order_routing_allowed: bool
    live_trading_allowed: bool
    metadata_annotations_present: bool = True
    action_id_annotation: str | None = None
    command_digest_annotation: str | None = None
    fencing_token_annotation: int | None = None
    target_replicas_annotation: int | None = None

    def __post_init__(self) -> None:
        for name in ("cluster", "namespace", "deployment_name", "deployment_uid", "service_account"):
            _validate_id(getattr(self, name), name)
        _validate_resource_version(self.resource_version)
        _validate_sha256(self.image_digest, "image_digest")
        _validate_sha256(self.config_digest, "config_digest")
        if self.generation <= 0:
            raise ValidationErrorV107("generation must be positive")
        for name in ("replicas", "ready_replicas", "available_replicas"):
            if getattr(self, name) < 0:
                raise ValidationErrorV107(f"{name} must be non-negative")
        if self.ready_replicas > self.replicas or self.available_replicas > self.replicas:
            raise ValidationErrorV107("ready or available replicas exceed desired replicas")
        if self.action_id_annotation is not None:
            _validate_id(self.action_id_annotation, "action_id_annotation")
        if self.command_digest_annotation is not None:
            _validate_hex(self.command_digest_annotation, "command_digest_annotation")
        if self.fencing_token_annotation is not None and self.fencing_token_annotation <= 0:
            raise ValidationErrorV107("invalid fencing token annotation")
        if self.target_replicas_annotation is not None and self.target_replicas_annotation < 0:
            raise ValidationErrorV107("invalid target replicas annotation")
        marker_values = (
            self.action_id_annotation,
            self.command_digest_annotation,
            self.fencing_token_annotation,
            self.target_replicas_annotation,
        )
        if not (all(value is None for value in marker_values) or all(value is not None for value in marker_values)):
            raise ValidationErrorV107("execution marker must be entirely absent or complete")
        if not self.metadata_annotations_present and any(value is not None for value in marker_values):
            raise ValidationErrorV107("execution marker cannot exist without annotations object")

    @property
    def snapshot_digest(self) -> str:
        return digest_v107(asdict(self))

    def idempotently_applied(self, command: SignedDeploymentExecutionCommandV107) -> bool:
        return (
            self.replicas == command.intent.target_replicas
            and self.action_id_annotation == command.intent.action_id
            and self.command_digest_annotation == command.command_digest
            and self.fencing_token_annotation == command.intent.fencing_token
            and self.target_replicas_annotation == command.intent.target_replicas
        )


@dataclass(frozen=True, slots=True)
class ExecutionGateV107:
    name: str
    passed: bool
    reason: str
    evidence_digest: str

    def __post_init__(self) -> None:
        _validate_id(self.name, "gate name")
        if not self.reason or len(self.reason) > 512:
            raise ValidationErrorV107("invalid gate reason")
        _validate_hex(self.evidence_digest, "evidence_digest")


@dataclass(frozen=True, slots=True)
class ExecutionGateSetV107:
    gates: tuple[ExecutionGateV107, ...]
    already_applied: bool = False

    def __post_init__(self) -> None:
        if not self.gates:
            raise ValidationErrorV107("gate set must not be empty")
        names = [gate.name for gate in self.gates]
        if len(names) != len(set(names)):
            raise ValidationErrorV107("duplicate gate names")

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def digest(self) -> str:
        return digest_v107({"gates": [asdict(gate) for gate in self.gates], "already_applied": self.already_applied})


def evaluate_execution_preflight_v107(
    *,
    policy: DeploymentExecutionPolicyV107,
    command: SignedDeploymentExecutionCommandV107,
    snapshot: DeploymentRuntimeSnapshotV107,
) -> ExecutionGateSetV107:
    intent = command.intent
    evidence = digest_v107({
        "policy": policy.policy_digest,
        "command": command.command_digest,
        "snapshot": snapshot.snapshot_digest,
    })
    already_applied = snapshot.idempotently_applied(command)
    marker_values = (
        snapshot.action_id_annotation,
        snapshot.command_digest_annotation,
        snapshot.fencing_token_annotation,
        snapshot.target_replicas_annotation,
    )
    marker_absent = all(value is None for value in marker_values)
    marker_complete = all(value is not None for value in marker_values)
    prior_complete_marker = (
        marker_complete
        and snapshot.fencing_token_annotation is not None
        and snapshot.fencing_token_annotation < intent.fencing_token
        and snapshot.action_id_annotation != intent.action_id
    )
    no_conflicting_marker = marker_absent or prior_complete_marker or already_applied
    fence_ok = (
        marker_absent
        or already_applied
        or (
            marker_complete
            and snapshot.fencing_token_annotation is not None
            and snapshot.fencing_token_annotation < intent.fencing_token
        )
    )
    gates = (
        ExecutionGateV107(
            "scope",
            snapshot.cluster == policy.cluster == intent.cluster
            and snapshot.namespace == policy.namespace == intent.namespace
            and snapshot.deployment_name == policy.deployment_name == intent.deployment_name
            and snapshot.deployment_uid == policy.deployment_uid == intent.deployment_uid
            and snapshot.service_account == policy.service_account == intent.service_account,
            "cluster, namespace, deployment UID and service account match",
            evidence,
        ),
        ExecutionGateV107(
            "release_identity",
            snapshot.image_digest == policy.expected_image_digest == intent.expected_image_digest
            and snapshot.config_digest == policy.expected_config_digest == intent.expected_config_digest,
            "image and configuration digests match the qualified release",
            evidence,
        ),
        ExecutionGateV107(
            "routing_boundary",
            not snapshot.external_order_routing_allowed and not snapshot.live_trading_allowed,
            "external order routing and live trading remain disabled",
            evidence,
        ),
        ExecutionGateV107(
            "annotations_object",
            snapshot.metadata_annotations_present,
            "metadata.annotations exists as an object before JSON Patch mutation",
            evidence,
        ),
        ExecutionGateV107(
            "resource_version",
            already_applied or snapshot.resource_version == intent.expected_resource_version,
            "resourceVersion matches the signed optimistic-lock precondition",
            evidence,
        ),
        ExecutionGateV107(
            "generation",
            already_applied or snapshot.generation == intent.expected_generation,
            "deployment generation matches the signed command",
            evidence,
        ),
        ExecutionGateV107(
            "current_replicas",
            already_applied or snapshot.replicas == intent.expected_current_replicas,
            "current replica count matches the signed command",
            evidence,
        ),
        ExecutionGateV107(
            "current_readiness",
            already_applied
            or (
                snapshot.ready_replicas == snapshot.replicas
                and snapshot.available_replicas == snapshot.replicas
            ),
            "all currently desired replicas are ready and available before mutation",
            evidence,
        ),
        ExecutionGateV107(
            "idempotency_marker",
            no_conflicting_marker,
            "no conflicting execution marker exists",
            evidence,
        ),
        ExecutionGateV107(
            "fencing",
            fence_ok,
            "fencing token is newer than any prior completed mutation marker",
            evidence,
        ),
    )
    return ExecutionGateSetV107(gates=gates, already_applied=already_applied)


def certify_full_rollout_v107(
    *,
    policy: DeploymentExecutionPolicyV107,
    command: SignedDeploymentExecutionCommandV107,
    snapshot: DeploymentRuntimeSnapshotV107,
) -> ExecutionGateSetV107:
    evidence = digest_v107({"command": command.command_digest, "snapshot": snapshot.snapshot_digest})
    gates = (
        ExecutionGateV107(
            "scope",
            snapshot.cluster == policy.cluster == command.intent.cluster
            and snapshot.namespace == policy.namespace == command.intent.namespace
            and snapshot.deployment_name == policy.deployment_name == command.intent.deployment_name
            and snapshot.deployment_uid == policy.deployment_uid == command.intent.deployment_uid
            and snapshot.service_account == policy.service_account == command.intent.service_account,
            "post-rollout scope remains bound to the signed deployment UID",
            evidence,
        ),
        ExecutionGateV107(
            "target_replicas",
            snapshot.replicas == command.intent.target_replicas,
            "desired replicas equal the signed target",
            evidence,
        ),
        ExecutionGateV107(
            "all_ready",
            snapshot.ready_replicas == command.intent.target_replicas,
            "every target replica is ready",
            evidence,
        ),
        ExecutionGateV107(
            "all_available",
            snapshot.available_replicas == command.intent.target_replicas,
            "every target replica is available",
            evidence,
        ),
        ExecutionGateV107(
            "release_identity",
            snapshot.image_digest == policy.expected_image_digest == command.intent.expected_image_digest
            and snapshot.config_digest == policy.expected_config_digest == command.intent.expected_config_digest,
            "release identity remains pinned after rollout",
            evidence,
        ),
        ExecutionGateV107(
            "routing_boundary",
            not snapshot.external_order_routing_allowed and not snapshot.live_trading_allowed,
            "external routing and live trading remain disabled",
            evidence,
        ),
        ExecutionGateV107(
            "execution_marker",
            snapshot.idempotently_applied(command),
            "deployment carries the exact action, command digest, target and fencing token",
            evidence,
        ),
    )
    return ExecutionGateSetV107(gates=gates, already_applied=snapshot.idempotently_applied(command))


@dataclass(frozen=True, slots=True)
class ExecutionReceiptV107:
    receipt_id: str
    command_id: str
    action_id: str
    worker_id: str
    status: ReceiptStatusV107
    observed_at: datetime
    command_digest: str
    pre_snapshot_digest: str
    post_snapshot_digest: str | None
    patch_digest: str | None
    mutation_attempted: bool
    reason: str
    executor_key_id: str
    signature: str

    def __post_init__(self) -> None:
        for name in ("receipt_id", "command_id", "action_id", "worker_id", "executor_key_id"):
            _validate_id(getattr(self, name), name)
        _ensure_utc(self.observed_at)
        _validate_hex(self.command_digest, "command_digest")
        _validate_hex(self.pre_snapshot_digest, "pre_snapshot_digest")
        if self.post_snapshot_digest is not None:
            _validate_hex(self.post_snapshot_digest, "post_snapshot_digest")
        if self.patch_digest is not None:
            _validate_hex(self.patch_digest, "patch_digest")
        _validate_hex(self.signature, "signature")
        if not self.reason or len(self.reason) > 512:
            raise ValidationErrorV107("invalid receipt reason")
        if self.status in {ReceiptStatusV107.APPLIED, ReceiptStatusV107.RECONCILED} and self.post_snapshot_digest is None:
            raise ValidationErrorV107("successful receipt requires post snapshot")
        if self.mutation_attempted and self.patch_digest is None:
            raise ValidationErrorV107("attempted mutation requires patch digest")
        if not self.mutation_attempted and self.patch_digest is not None:
            raise ValidationErrorV107("non-mutating receipt cannot contain patch digest")

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature")
        return payload

    @property
    def receipt_digest(self) -> str:
        return digest_v107(self.to_payload())

    @classmethod
    def sign(
        cls,
        *,
        receipt_id: str,
        command: SignedDeploymentExecutionCommandV107,
        worker_id: str,
        status: ReceiptStatusV107,
        observed_at: datetime,
        pre_snapshot_digest: str,
        post_snapshot_digest: str | None,
        patch_digest: str | None,
        mutation_attempted: bool,
        reason: str,
        executor_key_id: str,
        executor_secret: bytes,
    ) -> "ExecutionReceiptV107":
        _require_secret(executor_secret, "executor signing secret")
        unsigned = cls(
            receipt_id=receipt_id,
            command_id=command.intent.command_id,
            action_id=command.intent.action_id,
            worker_id=worker_id,
            status=status,
            observed_at=observed_at,
            command_digest=command.command_digest,
            pre_snapshot_digest=pre_snapshot_digest,
            post_snapshot_digest=post_snapshot_digest,
            patch_digest=patch_digest,
            mutation_attempted=mutation_attempted,
            reason=reason,
            executor_key_id=executor_key_id,
            signature="0" * 64,
        )
        signature = hmac.new(executor_secret, _canonical(unsigned.to_payload()), hashlib.sha256).hexdigest()
        return replace(unsigned, signature=signature)

    def verify(self, keyring: Mapping[str, bytes]) -> None:
        secret = keyring.get(self.executor_key_id)
        if secret is None:
            raise SignatureErrorV107("unknown executor key")
        _require_secret(secret, "executor key")
        expected = hmac.new(secret, _canonical(self.to_payload()), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise SignatureErrorV107("receipt signature mismatch")


@dataclass(frozen=True, slots=True)
class ExecutionJournalEventV107:
    sequence: int
    event_type: str
    observed_at: datetime
    payload_digest: str
    previous_digest: str
    event_digest: str


class ExecutionJournalV107:
    GENESIS = "0" * 64

    def __init__(self) -> None:
        self._events: list[ExecutionJournalEventV107] = []
        self._lock = threading.Lock()

    def append(self, event_type: str, payload: Any, observed_at: datetime) -> ExecutionJournalEventV107:
        _validate_id(event_type, "event_type")
        current = _ensure_utc(observed_at)
        payload_digest = digest_v107(payload)
        with self._lock:
            if self._events and current < self._events[-1].observed_at:
                raise ValidationErrorV107("journal time must be monotonic")
            previous = self._events[-1].event_digest if self._events else self.GENESIS
            sequence = len(self._events) + 1
            event_digest = digest_v107({
                "sequence": sequence,
                "event_type": event_type,
                "observed_at": current,
                "payload_digest": payload_digest,
                "previous_digest": previous,
            })
            event = ExecutionJournalEventV107(sequence, event_type, current, payload_digest, previous, event_digest)
            self._events.append(event)
            return event

    def verify(self) -> bool:
        previous = self.GENESIS
        previous_time: datetime | None = None
        for sequence, event in enumerate(self._events, start=1):
            if event.sequence != sequence or event.previous_digest != previous:
                return False
            if previous_time is not None and event.observed_at < previous_time:
                return False
            expected = digest_v107({
                "sequence": event.sequence,
                "event_type": event.event_type,
                "observed_at": event.observed_at,
                "payload_digest": event.payload_digest,
                "previous_digest": event.previous_digest,
            })
            if not hmac.compare_digest(expected, event.event_digest):
                return False
            previous = event.event_digest
            previous_time = event.observed_at
        return True

    @property
    def tail_digest(self) -> str:
        return self._events[-1].event_digest if self._events else self.GENESIS

    def snapshot(self) -> tuple[ExecutionJournalEventV107, ...]:
        return tuple(self._events)


@dataclass(slots=True)
class DeploymentExecutionCoordinatorV107:
    worker_id: str
    policy: DeploymentExecutionPolicyV107
    command: SignedDeploymentExecutionCommandV107
    state: ExecutionStateV107 = ExecutionStateV107.PENDING
    preflight: ExecutionGateSetV107 | None = None
    receipt: ExecutionReceiptV107 | None = None
    mutation_attempts: int = 0
    journal: ExecutionJournalV107 = field(default_factory=ExecutionJournalV107)

    def __post_init__(self) -> None:
        _validate_id(self.worker_id, "worker_id")

    def claim(self, observed_at: datetime) -> None:
        if self.state != ExecutionStateV107.PENDING:
            raise StateTransitionErrorV107("execution is not pending")
        self.state = ExecutionStateV107.CLAIMED
        self.journal.append("COMMAND_CLAIMED", {"command": self.command.command_digest, "worker": self.worker_id}, observed_at)

    def record_preflight(self, gates: ExecutionGateSetV107, observed_at: datetime) -> None:
        if self.state != ExecutionStateV107.CLAIMED:
            raise StateTransitionErrorV107("execution is not claimed")
        self.preflight = gates
        self.state = ExecutionStateV107.PREFLIGHT if gates.passed else ExecutionStateV107.QUARANTINED
        self.journal.append("EXECUTION_PREFLIGHT", {"gates": gates.digest}, observed_at)

    def start_mutation(self, patch_digest: str, observed_at: datetime) -> None:
        _validate_hex(patch_digest, "patch_digest")
        if self.state != ExecutionStateV107.PREFLIGHT or self.preflight is None or not self.preflight.passed:
            raise StateTransitionErrorV107("preflight did not pass")
        if self.preflight.already_applied:
            raise StateTransitionErrorV107("already-applied command does not require mutation")
        if self.mutation_attempts != 0:
            raise StateTransitionErrorV107("Kubernetes mutation may be attempted only once")
        self.mutation_attempts = 1
        self.state = ExecutionStateV107.MUTATION_STARTED
        self.journal.append("MUTATION_STARTED", {"command": self.command.command_digest, "patch": patch_digest}, observed_at)

    def start_verification(self, observed_at: datetime) -> None:
        if self.state not in {
            ExecutionStateV107.PREFLIGHT,
            ExecutionStateV107.MUTATION_STARTED,
            ExecutionStateV107.UNCERTAIN,
        }:
            raise StateTransitionErrorV107("execution cannot enter verification")
        self.state = ExecutionStateV107.VERIFYING
        self.journal.append("VERIFICATION_STARTED", {"command": self.command.command_digest}, observed_at)

    def mark_uncertain(self, observed_at: datetime, reason: str) -> None:
        if self.state not in {ExecutionStateV107.MUTATION_STARTED, ExecutionStateV107.VERIFYING}:
            raise StateTransitionErrorV107("execution cannot become uncertain")
        self.state = ExecutionStateV107.UNCERTAIN
        self.journal.append("EXECUTION_UNCERTAIN", {"reason": reason}, observed_at)

    def finish(self, receipt: ExecutionReceiptV107) -> None:
        if self.state not in {
            ExecutionStateV107.PREFLIGHT,
            ExecutionStateV107.VERIFYING,
            ExecutionStateV107.QUARANTINED,
            ExecutionStateV107.UNCERTAIN,
        }:
            raise StateTransitionErrorV107("execution cannot be finished")
        if receipt.command_digest != self.command.command_digest or receipt.worker_id != self.worker_id:
            raise ValidationErrorV107("receipt is not bound to this execution")
        self.receipt = receipt
        if receipt.status in {ReceiptStatusV107.APPLIED, ReceiptStatusV107.ALREADY_APPLIED, ReceiptStatusV107.RECONCILED}:
            self.state = ExecutionStateV107.SUCCEEDED
        elif receipt.status == ReceiptStatusV107.UNCERTAIN:
            self.state = ExecutionStateV107.UNCERTAIN
        else:
            self.state = ExecutionStateV107.FAILED
        self.journal.append(
            "EXECUTION_FINISHED",
            {"receipt": receipt.receipt_digest, "status": receipt.status},
            receipt.observed_at,
        )

    @property
    def state_digest(self) -> str:
        return digest_v107({
            "worker_id": self.worker_id,
            "policy": self.policy.policy_digest,
            "command": self.command.command_digest,
            "state": self.state,
            "preflight": self.preflight.digest if self.preflight else None,
            "receipt": self.receipt.receipt_digest if self.receipt else None,
            "mutation_attempts": self.mutation_attempts,
            "journal_tail": self.journal.tail_digest,
        })


EXTERNAL_ORDER_ROUTING_ALLOWED_V107 = False
LIVE_TRADING_ALLOWED_V107 = False
KUBERNETES_MUTATION_METHODS_V107 = ("PATCH",)
KUBERNETES_MUTATION_ATTEMPTS_V107 = 1
