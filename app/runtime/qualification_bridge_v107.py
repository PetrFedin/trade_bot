from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from app.runtime.rollout_execution_v107 import (
    DeploymentActionV107,
    DeploymentExecutionIntentV107,
    DeploymentExecutionPolicyV107,
    DeploymentRuntimeSnapshotV107,
    ValidationErrorV107,
    digest_v107,
)


class V106RolloutActionLike(Protocol):
    action_id: str
    qualification_id: str
    action: Any
    created_at: datetime
    evidence_digest: str
    state_digest: str
    idempotency_key: str
    status: Any
    attempt_count: int

    def verify(self, keyring: Mapping[str, bytes]) -> None: ...

    def to_payload(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class QualificationBridgeResultV107:
    intent: DeploymentExecutionIntentV107
    source_action_digest: str
    requires_independent_release_and_risk_attestations: bool = True


def build_execution_intent_from_v106(
    *,
    action: V106RolloutActionLike,
    action_keyring: Mapping[str, bytes],
    policy: DeploymentExecutionPolicyV107,
    snapshot: DeploymentRuntimeSnapshotV107,
    command_id: str,
    target_replicas: int,
    issued_at: datetime,
    not_before: datetime,
    expires_at: datetime,
    fencing_token: int,
    nonce: str,
) -> QualificationBridgeResultV107:
    """Convert a verified Schema 106 rollout action into an unsigned Schema 107 intent.

    Schema 106 proves the qualification decision. Schema 107 deliberately does not
    treat the two approver names inside the legacy action as two cryptographic
    approvals. Independent RELEASE and RISK attestations must still sign the new
    intent before execution.
    """

    try:
        action.verify(action_keyring)
    except Exception as exc:
        raise ValidationErrorV107("Schema 106 rollout action verification failed") from exc

    status = getattr(action.status, "value", action.status)
    if status != "PENDING" or action.attempt_count != 0:
        raise ValidationErrorV107("Schema 106 action must be pending and unclaimed")
    action_value = getattr(action.action, "value", action.action)
    try:
        mapped_action = DeploymentActionV107(action_value)
    except ValueError as exc:
        raise ValidationErrorV107("unsupported Schema 106 rollout action") from exc

    if (
        snapshot.cluster != policy.cluster
        or snapshot.namespace != policy.namespace
        or snapshot.deployment_name != policy.deployment_name
        or snapshot.deployment_uid != policy.deployment_uid
        or snapshot.service_account != policy.service_account
    ):
        raise ValidationErrorV107("Schema 106 action snapshot does not match execution policy")
    if snapshot.image_digest != policy.expected_image_digest or snapshot.config_digest != policy.expected_config_digest:
        raise ValidationErrorV107("Schema 106 action snapshot release identity mismatch")
    if snapshot.external_order_routing_allowed or snapshot.live_trading_allowed:
        raise ValidationErrorV107("Schema 106 bridge requires routing and live trading disabled")

    source_action_digest = digest_v107({
        "payload": action.to_payload(),
        "signature": getattr(action, "signature", None),
    })
    intent = DeploymentExecutionIntentV107(
        command_id=command_id,
        action_id=action.action_id,
        qualification_id=action.qualification_id,
        qualification_action_digest=source_action_digest,
        action=mapped_action,
        cluster=policy.cluster,
        namespace=policy.namespace,
        deployment_name=policy.deployment_name,
        deployment_uid=policy.deployment_uid,
        service_account=policy.service_account,
        expected_resource_version=snapshot.resource_version,
        expected_generation=snapshot.generation,
        expected_current_replicas=snapshot.replicas,
        target_replicas=target_replicas,
        expected_image_digest=policy.expected_image_digest,
        expected_config_digest=policy.expected_config_digest,
        qualification_evidence_digest=action.evidence_digest,
        qualification_state_digest=action.state_digest,
        issued_at=issued_at,
        not_before=not_before,
        expires_at=expires_at,
        idempotency_key=action.idempotency_key,
        fencing_token=fencing_token,
        nonce=nonce,
    )
    intent.verify_policy(policy)
    return QualificationBridgeResultV107(
        intent=intent,
        source_action_digest=source_action_digest,
    )


LEGACY_APPROVER_NAMES_ARE_CRYPTOGRAPHIC_APPROVALS_V107 = False
INDEPENDENT_RELEASE_AND_RISK_ATTESTATIONS_REQUIRED_V107 = True
