from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Protocol, Sequence


class CursorV104(Protocol):
    rowcount: int
    def execute(self, query: str, params: Sequence[Any] | None = None) -> Any: ...
    def fetchone(self) -> Sequence[Any] | None: ...
    def fetchall(self) -> Sequence[Sequence[Any]]: ...
    def close(self) -> None: ...


class ConnectionV104(Protocol):
    def cursor(self) -> CursorV104: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ClaimedWorkV104:
    claim_id: str
    campaign_id: str
    run_id: str
    generation: int
    fencing_token: int
    signed_claim_json: str


class PostgresWorkerRepositoryV104:
    def __init__(self, connection: ConnectionV104) -> None:
        self.connection = connection

    @contextmanager
    def transaction(self) -> Iterator[CursorV104]:
        cursor = self.connection.cursor()
        try:
            yield cursor
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()
        finally:
            cursor.close()

    def claim_next(self, worker_id: str, deployment_id: str, now: datetime) -> ClaimedWorkV104 | None:
        query = """
        WITH candidate AS (
            SELECT claim_id
            FROM astra_v104.worker_claim
            WHERE state = 'READY' AND not_before <= %s AND expires_at > %s
            ORDER BY not_before, claim_id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE astra_v104.worker_claim AS claim
        SET state = 'CLAIMED', worker_id = %s, deployment_id = %s, claimed_at = %s
        FROM candidate
        WHERE claim.claim_id = candidate.claim_id
        RETURNING claim.claim_id, claim.campaign_id, claim.run_id,
                  claim.generation, claim.fencing_token, claim.signed_claim_json::text
        """
        with self.transaction() as cursor:
            cursor.execute(query, (now, now, worker_id, deployment_id, now))
            row = cursor.fetchone()
        return ClaimedWorkV104(*row) if row else None

    def heartbeat(self, claim_id: str, worker_id: str, generation: int, fencing_token: int, sequence: int, observed_at: datetime) -> bool:
        query = """
        UPDATE astra_v104.worker_claim
        SET heartbeat_sequence = %s, heartbeat_at = %s
        WHERE claim_id = %s AND worker_id = %s AND generation = %s
          AND fencing_token = %s AND state IN ('CLAIMED','RUNNING','SPOOLING','UPLOADING')
          AND heartbeat_sequence < %s
        """
        with self.transaction() as cursor:
            cursor.execute(query, (sequence, observed_at, claim_id, worker_id, generation, fencing_token, sequence))
            updated = cursor.rowcount == 1
        return updated

    def record_spool(self, record_id: str, claim_id: str, digest: str, byte_length: int, retention_until: datetime) -> None:
        query = """
        INSERT INTO astra_v104.evidence_spool
            (record_id, claim_id, payload_digest, byte_length, retention_until, state)
        VALUES (%s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT (record_id) DO NOTHING
        """
        with self.transaction() as cursor:
            cursor.execute(query, (record_id, claim_id, digest, byte_length, retention_until))

    def enqueue_dlq(self, record_id: str, claim_id: str, reason: str, detail: str, occurred_at: datetime) -> None:
        query = """
        INSERT INTO astra_v104.worker_dead_letter
            (record_id, claim_id, reason, detail, occurred_at, released)
        VALUES (%s, %s, %s, %s, %s, false)
        ON CONFLICT (record_id) DO NOTHING
        """
        with self.transaction() as cursor:
            cursor.execute(query, (record_id, claim_id, reason, detail, occurred_at))

    def release_dlq(self, record_id: str, release_sequence: int, operator: str, reason: str, released_at: datetime) -> None:
        if not operator or release_sequence <= 0:
            raise ValueError("operator and positive release sequence required")
        query = """
        INSERT INTO astra_v104.worker_dead_letter_release
            (record_id, release_sequence, released_by, released_at, reason, previous_digest, release_digest)
        VALUES (%s, %s, %s, %s, %s,
                repeat('0', 64),
                encode(digest(concat_ws('|', %s, %s, %s, %s), 'sha256'), 'hex'))
        ON CONFLICT (record_id, release_sequence) DO NOTHING
        """
        with self.transaction() as cursor:
            cursor.execute(query, (record_id, release_sequence, operator, released_at, reason, record_id, release_sequence, operator, released_at))
