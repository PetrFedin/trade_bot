from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Protocol, Sequence


class CursorV105(Protocol):
    rowcount: int
    def execute(self, query: str, params: Sequence[Any] | None = None) -> Any: ...
    def fetchone(self) -> Sequence[Any] | None: ...
    def fetchall(self) -> Sequence[Sequence[Any]]: ...
    def close(self) -> None: ...


class ConnectionV105(Protocol):
    def cursor(self) -> CursorV105: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class FleetRepositoryErrorV105(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimedFleetTaskV105:
    task_id: str
    task_type: str
    generation: int
    fencing_token: int


class PostgresFleetRepositoryV105:
    def __init__(self, connection: ConnectionV105) -> None:
        self.connection = connection

    @contextmanager
    def transaction(self) -> Iterator[CursorV105]:
        cursor = self.connection.cursor()
        try:
            yield cursor
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def consume_enrollment_nonce(self, token_id: str, nonce: str, consumed_at: datetime) -> bool:
        query = """
            INSERT INTO astra_v105.enrollment_replay_guard(token_id, nonce, consumed_at)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
        """
        with self.transaction() as cursor:
            cursor.execute(query, (token_id, nonce, consumed_at))
            return cursor.rowcount == 1

    def record_worker(self, worker_id: str, deployment_id: str, zone: str, certificate_fingerprint: str, generation: int, state: str, observed_at: datetime) -> None:
        query = """
            INSERT INTO astra_v105.fleet_worker(worker_id, deployment_id, zone, certificate_fingerprint, identity_generation, state, observed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (worker_id) DO UPDATE SET
                deployment_id = EXCLUDED.deployment_id,
                zone = EXCLUDED.zone,
                certificate_fingerprint = EXCLUDED.certificate_fingerprint,
                identity_generation = EXCLUDED.identity_generation,
                state = EXCLUDED.state,
                observed_at = EXCLUDED.observed_at
            WHERE astra_v105.fleet_worker.identity_generation <= EXCLUDED.identity_generation
        """
        with self.transaction() as cursor:
            cursor.execute(query, (worker_id, deployment_id, zone, certificate_fingerprint, generation, state, observed_at))
            if cursor.rowcount != 1:
                raise FleetRepositoryErrorV105("stale worker generation")

    def record_heartbeat(self, worker_id: str, generation: int, sequence: int, observed_at: datetime) -> None:
        query = """
            UPDATE astra_v105.fleet_worker
               SET heartbeat_sequence = %s, last_heartbeat_at = %s, observed_at = %s
             WHERE worker_id = %s
               AND identity_generation = %s
               AND heartbeat_sequence < %s
        """
        with self.transaction() as cursor:
            cursor.execute(query, (sequence, observed_at, observed_at, worker_id, generation, sequence))
            if cursor.rowcount != 1:
                raise FleetRepositoryErrorV105("heartbeat fencing rejected")

    def claim_task(self, owner_id: str, now: datetime) -> ClaimedFleetTaskV105 | None:
        query = """
            WITH candidate AS (
                SELECT task_id
                  FROM astra_v105.fleet_task
                 WHERE state = 'PENDING' AND not_before <= %s
                 ORDER BY priority DESC, created_at, task_id
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            )
            UPDATE astra_v105.fleet_task AS task
               SET state = 'CLAIMED', owner_id = %s, claimed_at = %s,
                   generation = generation + 1, fencing_token = fencing_token + 1
              FROM candidate
             WHERE task.task_id = candidate.task_id
         RETURNING task.task_id, task.task_type, task.generation, task.fencing_token
        """
        with self.transaction() as cursor:
            cursor.execute(query, (now, owner_id, now))
            row = cursor.fetchone()
            if row is None:
                return None
            return ClaimedFleetTaskV105(str(row[0]), str(row[1]), int(row[2]), int(row[3]))

    def append_containment(self, containment_id: str, epoch: int, scope: str, target: str, reason: str, activated_at: datetime) -> None:
        query = """
            INSERT INTO astra_v105.fleet_containment(containment_id, epoch, scope, target, reason, activated_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        with self.transaction() as cursor:
            cursor.execute(query, (containment_id, epoch, scope, target, reason, activated_at))

    def append_containment_release(self, containment_id: str, epoch: int, evidence_digest: str, operator_a: str, operator_b: str, released_at: datetime) -> None:
        query = """
            INSERT INTO astra_v105.fleet_containment_release(containment_id, epoch, evidence_digest, operator_a, operator_b, released_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        with self.transaction() as cursor:
            cursor.execute(query, (containment_id, epoch, evidence_digest, operator_a, operator_b, released_at))

    def append_scale_decision(self, decision_id: str, digest: str, current_replicas: int, desired_replicas: int, reason: str, observed_at: datetime) -> None:
        query = """
            INSERT INTO astra_v105.autoscale_decision(decision_id, digest, current_replicas, desired_replicas, reason, observed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        with self.transaction() as cursor:
            cursor.execute(query, (decision_id, digest, current_replicas, desired_replicas, reason, observed_at))

    def record_evidence_object(self, object_key: str, object_sha256: str, size_bytes: int, upload_id: str, recorded_at: datetime) -> None:
        query = """
            INSERT INTO astra_v105.evidence_object(object_key, object_sha256, size_bytes, upload_id, recorded_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (object_key) DO NOTHING
        """
        with self.transaction() as cursor:
            cursor.execute(query, (object_key, object_sha256, size_bytes, upload_id, recorded_at))
            if cursor.rowcount != 1:
                raise FleetRepositoryErrorV105("evidence object replay conflict")
