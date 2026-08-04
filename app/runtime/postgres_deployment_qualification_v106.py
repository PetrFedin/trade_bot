from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Iterator, Protocol, Sequence


class CursorV106(Protocol):
    rowcount: int
    def execute(self, query: str, params: Sequence[Any] | None = None) -> Any: ...
    def fetchone(self) -> Sequence[Any] | None: ...
    def close(self) -> Any: ...


class ConnectionV106(Protocol):
    def cursor(self) -> CursorV106: ...
    def commit(self) -> Any: ...
    def rollback(self) -> Any: ...


class PostgresRepositoryErrorV106(RuntimeError):
    pass


class StaleFenceErrorV106(PostgresRepositoryErrorV106):
    pass


@dataclass(frozen=True, slots=True)
class ClaimedRolloutActionV106:
    action_id: str
    qualification_id: str
    action_type: str
    generation: int
    fencing_token: int
    payload_digest: str


class PostgresDeploymentQualificationRepositoryV106:
    def __init__(self, connection: ConnectionV106) -> None:
        self._connection = connection

    @contextmanager
    def _transaction(self) -> Iterator[CursorV106]:
        cursor = self._connection.cursor()
        try:
            yield cursor
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        finally:
            cursor.close()

    def consume_manifest_replay(self, *, manifest_id: str, nonce: str, consumed_at: datetime) -> None:
        query = """
            INSERT INTO astra_v106.manifest_replay_guard
                (manifest_id, nonce, consumed_at)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """
        with self._transaction() as cursor:
            cursor.execute(query, (manifest_id, nonce, consumed_at))
            if cursor.rowcount != 1:
                raise PostgresRepositoryErrorV106("manifest or nonce replay detected")

    def create_qualification(
        self,
        *,
        qualification_id: str,
        manifest_id: str,
        policy_digest: str,
        manifest_digest: str,
        generation: int,
        state: str,
        created_at: datetime,
    ) -> None:
        query = """
            INSERT INTO astra_v106.deployment_qualification
                (qualification_id, manifest_id, policy_digest, manifest_digest,
                 generation, state, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        with self._transaction() as cursor:
            cursor.execute(query, (
                qualification_id,
                manifest_id,
                policy_digest,
                manifest_digest,
                generation,
                state,
                created_at,
                created_at,
            ))
            if cursor.rowcount != 1:
                raise PostgresRepositoryErrorV106("qualification insert failed")

    def append_event(
        self,
        *,
        qualification_id: str,
        sequence: int,
        event_type: str,
        observed_at: datetime,
        payload_digest: str,
        previous_digest: str,
        event_digest: str,
    ) -> None:
        query = """
            INSERT INTO astra_v106.qualification_event
                (qualification_id, sequence, event_type, observed_at,
                 payload_digest, previous_digest, event_digest)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        with self._transaction() as cursor:
            cursor.execute(query, (
                qualification_id,
                sequence,
                event_type,
                observed_at,
                payload_digest,
                previous_digest,
                event_digest,
            ))
            if cursor.rowcount != 1:
                raise PostgresRepositoryErrorV106("qualification event insert failed")

    def append_observation(
        self,
        *,
        qualification_id: str,
        sample_id: str,
        observed_at: datetime,
        sample_digest: str,
        gate_digest: str,
        passed: bool,
    ) -> None:
        query = """
            INSERT INTO astra_v106.observation_sample
                (qualification_id, sample_id, observed_at, sample_digest,
                 gate_digest, passed)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        with self._transaction() as cursor:
            cursor.execute(query, (
                qualification_id,
                sample_id,
                observed_at,
                sample_digest,
                gate_digest,
                passed,
            ))
            if cursor.rowcount != 1:
                raise PostgresRepositoryErrorV106("observation insert failed")

    def enqueue_rollout_action(
        self,
        *,
        action_id: str,
        qualification_id: str,
        action_type: str,
        generation: int,
        fencing_token: int,
        idempotency_key: str,
        payload_digest: str,
        signature: str,
        created_at: datetime,
    ) -> None:
        query = """
            INSERT INTO astra_v106.rollout_action_outbox
                (action_id, qualification_id, action_type, generation,
                 fencing_token, idempotency_key, payload_digest, signature,
                 status, attempt_count, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', 0, %s)
        """
        with self._transaction() as cursor:
            cursor.execute(query, (
                action_id,
                qualification_id,
                action_type,
                generation,
                fencing_token,
                idempotency_key,
                payload_digest,
                signature,
                created_at,
            ))
            if cursor.rowcount != 1:
                raise PostgresRepositoryErrorV106("rollout action insert failed")

    def claim_rollout_action(
        self,
        *,
        worker_id: str,
        generation: int,
        fencing_token: int,
        claimed_at: datetime,
    ) -> ClaimedRolloutActionV106 | None:
        select_query = """
            SELECT action_id, qualification_id, action_type, generation,
                   fencing_token, payload_digest
            FROM astra_v106.rollout_action_outbox
            WHERE status = 'PENDING'
              AND generation = %s
              AND fencing_token = %s
            ORDER BY created_at, action_id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        """
        update_query = """
            UPDATE astra_v106.rollout_action_outbox
            SET status = 'CLAIMED', claimed_by = %s, claimed_at = %s,
                attempt_count = attempt_count + 1
            WHERE action_id = %s
              AND status = 'PENDING'
              AND generation = %s
              AND fencing_token = %s
              AND attempt_count = 0
        """
        with self._transaction() as cursor:
            cursor.execute(select_query, (generation, fencing_token))
            row = cursor.fetchone()
            if row is None:
                return None
            action = ClaimedRolloutActionV106(
                action_id=str(row[0]),
                qualification_id=str(row[1]),
                action_type=str(row[2]),
                generation=int(row[3]),
                fencing_token=int(row[4]),
                payload_digest=str(row[5]),
            )
            cursor.execute(update_query, (
                worker_id,
                claimed_at,
                action.action_id,
                generation,
                fencing_token,
            ))
            if cursor.rowcount != 1:
                raise StaleFenceErrorV106("rollout action claim lost fencing race")
            return action

    def acknowledge_rollout_action(
        self,
        *,
        action_id: str,
        generation: int,
        fencing_token: int,
        success: bool,
        receipt_digest: str,
        acknowledged_at: datetime,
    ) -> None:
        query = """
            UPDATE astra_v106.rollout_action_outbox
            SET status = %s, receipt_digest = %s, acknowledged_at = %s
            WHERE action_id = %s
              AND generation = %s
              AND fencing_token = %s
              AND status = 'CLAIMED'
              AND attempt_count = 1
        """
        with self._transaction() as cursor:
            cursor.execute(query, (
                "ACKED" if success else "FAILED",
                receipt_digest,
                acknowledged_at,
                action_id,
                generation,
                fencing_token,
            ))
            if cursor.rowcount != 1:
                raise StaleFenceErrorV106("rollout action acknowledgement rejected")

    def record_certificate_drill_event(
        self,
        *,
        drill_id: str,
        sequence: int,
        state: str,
        worker_id: str,
        identity_generation: int,
        evidence_digest: str,
        observed_at: datetime,
    ) -> None:
        query = """
            INSERT INTO astra_v106.certificate_drill_event
                (drill_id, sequence, state, worker_id, identity_generation,
                 evidence_digest, observed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        with self._transaction() as cursor:
            cursor.execute(query, (
                drill_id,
                sequence,
                state,
                worker_id,
                identity_generation,
                evidence_digest,
                observed_at,
            ))
            if cursor.rowcount != 1:
                raise PostgresRepositoryErrorV106("certificate drill event insert failed")

    def record_disaster_recovery_event(
        self,
        *,
        drill_id: str,
        sequence: int,
        state: str,
        backup_id: str,
        evidence: dict[str, Any],
        observed_at: datetime,
    ) -> None:
        query = """
            INSERT INTO astra_v106.disaster_recovery_event
                (drill_id, sequence, state, backup_id, evidence_json, observed_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        """
        with self._transaction() as cursor:
            cursor.execute(query, (
                drill_id,
                sequence,
                state,
                backup_id,
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                observed_at,
            ))
            if cursor.rowcount != 1:
                raise PostgresRepositoryErrorV106("disaster recovery event insert failed")
