from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from app.runtime.rollout_execution_v107 import (
    ApprovalAttestationV107,
    ApprovalRoleV107,
    DeploymentActionV107,
    DeploymentExecutionIntentV107,
    DeploymentExecutionPolicyV107,
    DeploymentRuntimeSnapshotV107,
    SignedDeploymentExecutionCommandV107,
)

UTC = timezone.utc
IMAGE = "sha256:" + "1" * 64
CONFIG = "sha256:" + "2" * 64
EVIDENCE = "3" * 64
STATE = "4" * 64
RELEASE_SECRET = b"r" * 32
RISK_SECRET = b"k" * 32
CONTROLLER_SECRET = b"c" * 32
EXECUTOR_SECRET = b"e" * 32
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def policy() -> DeploymentExecutionPolicyV107:
    return DeploymentExecutionPolicyV107(
        cluster="cluster-a",
        namespace="astra-prod",
        deployment_name="trade-bot-workers",
        deployment_uid="uid-123",
        service_account="astra-worker",
        expected_image_digest=IMAGE,
        expected_config_digest=CONFIG,
        min_replicas=2,
        max_replicas=10,
        rollback_replicas=1,
        max_command_lifetime_seconds=600,
        max_clock_skew_seconds=5,
        max_readiness_wait_seconds=10,
        recovery_claim_ttl_seconds=30,
    )


@pytest.fixture
def intent() -> DeploymentExecutionIntentV107:
    return DeploymentExecutionIntentV107(
        command_id="cmd-001",
        action_id="action-001",
        qualification_id="qual-001",
        qualification_action_digest="5" * 64,
        action=DeploymentActionV107.PROMOTE,
        cluster="cluster-a",
        namespace="astra-prod",
        deployment_name="trade-bot-workers",
        deployment_uid="uid-123",
        service_account="astra-worker",
        expected_resource_version="100",
        expected_generation=7,
        expected_current_replicas=2,
        target_replicas=4,
        expected_image_digest=IMAGE,
        expected_config_digest=CONFIG,
        qualification_evidence_digest=EVIDENCE,
        qualification_state_digest=STATE,
        issued_at=NOW,
        not_before=NOW,
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key="idem-001",
        fencing_token=11,
        nonce="nonce-001",
    )


@pytest.fixture
def command(intent: DeploymentExecutionIntentV107) -> SignedDeploymentExecutionCommandV107:
    release = ApprovalAttestationV107.sign(
        approval_id="approval-release",
        intent=intent,
        approver_id="alice",
        role=ApprovalRoleV107.RELEASE,
        signed_at=NOW,
        nonce="approval-nonce-release",
        key_id="release-key",
        secret=RELEASE_SECRET,
    )
    risk = ApprovalAttestationV107.sign(
        approval_id="approval-risk",
        intent=intent,
        approver_id="bob",
        role=ApprovalRoleV107.RISK,
        signed_at=NOW,
        nonce="approval-nonce-risk",
        key_id="risk-key",
        secret=RISK_SECRET,
    )
    return SignedDeploymentExecutionCommandV107.sign(
        intent=intent,
        approvals=(release, risk),
        controller_key_id="controller-key",
        controller_secret=CONTROLLER_SECRET,
    )


@pytest.fixture
def snapshot() -> DeploymentRuntimeSnapshotV107:
    return DeploymentRuntimeSnapshotV107(
        cluster="cluster-a",
        namespace="astra-prod",
        deployment_name="trade-bot-workers",
        deployment_uid="uid-123",
        service_account="astra-worker",
        resource_version="100",
        generation=7,
        replicas=2,
        ready_replicas=2,
        available_replicas=2,
        image_digest=IMAGE,
        config_digest=CONFIG,
        external_order_routing_allowed=False,
        live_trading_allowed=False,
        metadata_annotations_present=True,
    )


def deployment_document(
    *,
    replicas: int = 2,
    ready: int = 2,
    available: int = 2,
    resource_version: str = "100",
    generation: int = 7,
    annotations: dict[str, str] | None = None,
) -> bytes:
    base_annotations = {
        "astra.openai.com/config-digest": CONFIG,
        "astra.openai.com/external-order-routing-allowed": "false",
        "astra.openai.com/live-trading-allowed": "false",
    }
    if annotations:
        base_annotations.update(annotations)
    return json.dumps(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "trade-bot-workers",
                "namespace": "astra-prod",
                "uid": "uid-123",
                "resourceVersion": resource_version,
                "generation": generation,
                "annotations": base_annotations,
            },
            "spec": {
                "replicas": replicas,
                "template": {
                    "spec": {
                        "serviceAccountName": "astra-worker",
                        "containers": [{"name": "worker", "image": "registry/trade-bot@" + IMAGE}],
                    }
                },
            },
            "status": {"readyReplicas": ready, "availableReplicas": available},
        },
        sort_keys=True,
    ).encode()
