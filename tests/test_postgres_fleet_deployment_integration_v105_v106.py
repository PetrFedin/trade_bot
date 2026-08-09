from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from app.runtime.postgres_deployment_qualification_v106 import (
    PostgresDeploymentQualificationRepositoryV106,
    PostgresRepositoryErrorV106,
    StaleFenceErrorV106,
)
from app.runtime.postgres_fleet_operations_v105 import (
    FleetRepositoryErrorV105,
    PostgresFleetRepositoryV105,
)

DSN = os.getenv("ASTRA_TEST_FLEET_DEPLOYMENT_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ASTRA_TEST_FLEET_DEPLOYMENT_DSN is required")
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64


def connect():
    import psycopg

    assert DSN is not None
    return psycopg.connect(DSN)


def clean_v105() -> None:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE astra_v105.worker_identity_rotation,
                         astra_v105.worker_heartbeat_event,
                         astra_v105.fleet_containment_release,
                         astra_v105.fleet_containment,
                         astra_v105.autoscale_decision,
                         astra_v105.evidence_object,
                         astra_v105.fleet_event,
                         astra_v105.fleet_task,
                         astra_v105.fleet_worker,
                         astra_v105.enrollment_replay_guard
                RESTART IDENTITY CASCADE
                """
            )
        connection.commit()
    finally:
        connection.close()


def clean_v106() -> None:
    connection = connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE astra_v106.certificate_drill_event,
                         astra_v106.certificate_drill,
                         astra_v106.disaster_recovery_event,
                         astra_v106.disaster_recovery_drill,
                         astra_v106.backup_manifest,
                         astra_v106.rollout_action_outbox,
                         astra_v106.qualification_event,
                         astra_v106.observation_sample,
                         astra_v106.preflight_gate_set,
                         astra_v106.kubernetes_snapshot,
                         astra_v106.deployment_qualification,
                         astra_v106.deployment_manifest,
                         astra_v106.manifest_replay_guard
                RESTART IDENTITY CASCADE
                """
            )
        connection.commit()
    finally:
        connection.close()


def test_v105_repository_real_postgres_replay_fencing_claim_and_append_only() -> None:
    clean_v105()
    connection = connect()
    repository = PostgresFleetRepositoryV105(connection)
    try:
        assert repository.consume_enrollment_nonce("token-1", "nonce-1", NOW) is True
        assert repository.consume_enrollment_nonce("token-1", "nonce-1", NOW) is False

        repository.record_worker("worker-1", "deployment-1", "zone-a", HEX_A, 2, "ACTIVE", NOW)
        with pytest.raises(FleetRepositoryErrorV105, match="stale"):
            repository.record_worker(
                "worker-1", "deployment-1", "zone-a", HEX_B, 1, "ACTIVE", NOW
            )

        repository.record_heartbeat("worker-1", 2, 1, NOW + timedelta(seconds=1))
        with pytest.raises(FleetRepositoryErrorV105, match="heartbeat"):
            repository.record_heartbeat("worker-1", 2, 1, NOW + timedelta(seconds=2))

        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO astra_v105.fleet_task(
                    task_id, task_type, priority, not_before, created_at, payload_digest
                ) VALUES (%s, 'DRAIN', 10, %s, %s, %s)
                """,
                ("task-1", NOW - timedelta(seconds=1), NOW, HEX_C),
            )
        connection.commit()

        claimed = repository.claim_task("owner-1", NOW)
        assert claimed is not None
        assert (claimed.task_id, claimed.generation, claimed.fencing_token) == ("task-1", 1, 1)
        assert repository.claim_task("owner-2", NOW) is None

        repository.record_evidence_object("evidence-1", HEX_A, 128, "upload-1", NOW)
        with pytest.raises(FleetRepositoryErrorV105, match="replay"):
            repository.record_evidence_object("evidence-1", HEX_A, 128, "upload-1", NOW)

        with connection.cursor() as cursor:
            with pytest.raises(Exception, match="append-only"):
                cursor.execute(
                    "UPDATE astra_v105.evidence_object SET size_bytes = 129 WHERE object_key = %s",
                    ("evidence-1",),
                )
        connection.rollback()
    finally:
        connection.close()


def insert_v106_manifest(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO astra_v106.deployment_manifest(
                manifest_id, rollout_id, deployment_id, fleet_id, environment, generation,
                image_digest, config_digest, replicas, canary_replicas, issued_at, not_before,
                expires_at, nonce, key_id, signature, manifest_digest
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            """,
            (
                "manifest-1",
                "rollout-1",
                "deployment-1",
                "fleet-1",
                "qualification-local",
                7,
                f"sha256:{HEX_A}",
                f"sha256:{HEX_B}",
                3,
                1,
                NOW,
                NOW,
                NOW + timedelta(hours=1),
                "manifest-nonce-1",
                "key-1",
                HEX_C,
                HEX_A,
            ),
        )
    connection.commit()


def test_v106_repository_real_postgres_replay_fencing_ack_and_append_only() -> None:
    clean_v106()
    connection = connect()
    repository = PostgresDeploymentQualificationRepositoryV106(connection)
    try:
        insert_v106_manifest(connection)
        repository.consume_manifest_replay(
            manifest_id="manifest-1", nonce="replay-nonce-1", consumed_at=NOW
        )
        with pytest.raises(PostgresRepositoryErrorV106, match="replay"):
            repository.consume_manifest_replay(
                manifest_id="manifest-1", nonce="replay-nonce-1", consumed_at=NOW
            )

        repository.create_qualification(
            qualification_id="qualification-1",
            manifest_id="manifest-1",
            policy_digest=HEX_B,
            manifest_digest=HEX_A,
            generation=7,
            state="PLANNED",
            created_at=NOW,
        )
        repository.append_event(
            qualification_id="qualification-1",
            sequence=1,
            event_type="START",
            observed_at=NOW,
            payload_digest=HEX_A,
            previous_digest=HEX_B,
            event_digest=HEX_C,
        )
        repository.append_observation(
            qualification_id="qualification-1",
            sample_id="sample-1",
            observed_at=NOW,
            sample_digest=HEX_B,
            gate_digest=HEX_C,
            passed=True,
        )
        repository.enqueue_rollout_action(
            action_id="action-1",
            qualification_id="qualification-1",
            action_type="PROMOTE",
            generation=7,
            fencing_token=9,
            idempotency_key="qualification-1:PROMOTE:7",
            payload_digest=HEX_A,
            signature=HEX_B,
            created_at=NOW,
        )

        claimed = repository.claim_rollout_action(
            worker_id="worker-1", generation=7, fencing_token=9, claimed_at=NOW
        )
        assert claimed is not None
        assert claimed.action_id == "action-1"
        assert repository.claim_rollout_action(
            worker_id="worker-2", generation=7, fencing_token=9, claimed_at=NOW
        ) is None

        with pytest.raises(StaleFenceErrorV106, match="acknowledgement"):
            repository.acknowledge_rollout_action(
                action_id="action-1",
                generation=7,
                fencing_token=10,
                success=True,
                receipt_digest=HEX_C,
                acknowledged_at=NOW + timedelta(seconds=1),
            )
        repository.acknowledge_rollout_action(
            action_id="action-1",
            generation=7,
            fencing_token=9,
            success=True,
            receipt_digest=HEX_C,
            acknowledged_at=NOW + timedelta(seconds=1),
        )
        with pytest.raises(StaleFenceErrorV106, match="acknowledgement"):
            repository.acknowledge_rollout_action(
                action_id="action-1",
                generation=7,
                fencing_token=9,
                success=True,
                receipt_digest=HEX_C,
                acknowledged_at=NOW + timedelta(seconds=2),
            )

        with connection.cursor() as cursor:
            with pytest.raises(Exception, match="append-only"):
                cursor.execute(
                    """
                    UPDATE astra_v106.qualification_event
                       SET event_type = 'TAMPERED'
                     WHERE qualification_id = %s AND sequence = 1
                    """,
                    ("qualification-1",),
                )
        connection.rollback()
    finally:
        connection.close()
