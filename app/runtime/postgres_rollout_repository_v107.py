from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from app.runtime.rollout_execution_v107 import (
    ApprovalAttestationV107,
    ApprovalRoleV107,
    DeploymentActionV107,
    DeploymentExecutionIntentV107,
    ExecutionReceiptV107,
    ExecutionStateV107,
    ReceiptStatusV107,
    SignedDeploymentExecutionCommandV107,
    ValidationErrorV107,
    digest_v107,
)

UTC = timezone.utc


class PostgreSQLRepositoryErrorV107(RuntimeError):
    pass


class PostgreSQLConflictV107(PostgreSQLRepositoryErrorV107):
    pass


class PostgreSQLNotFoundV107(PostgreSQLRepositoryErrorV107):
    pass


class CursorV107(Protocol):
    rowcount: int

    def execute(self, query: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Any: ...

    def close(self) -> Any: ...


class ConnectionV107(Protocol):
    def cursor(self) -> CursorV107: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...

    def close(self) -> Any: ...


ConnectionFactoryV107 = Callable[[], ConnectionV107]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationErrorV107("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _ensure_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    raise TypeError(type(value).__name__)


def command_to_json_v107(command: SignedDeploymentExecutionCommandV107) -> str:
    return json.dumps(
        {
            "intent": asdict(command.intent),
            "approvals": [asdict(approval) for approval in command.approvals],
            "controller_key_id": command.controller_key_id,
            "controller_signature": command.controller_signature,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _parse_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise PostgreSQLRepositoryErrorV107(f"{name} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PostgreSQLRepositoryErrorV107(f"invalid {name}") from exc
    return _ensure_utc(parsed)


def command_from_json_v107(value: str) -> SignedDeploymentExecutionCommandV107:
    try:
        document = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PostgreSQLRepositoryErrorV107("invalid stored command JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("intent"), dict):
        raise PostgreSQLRepositoryErrorV107("stored command has invalid shape")
    try:
        intent_raw = dict(document["intent"])
        for name in ("issued_at", "not_before", "expires_at"):
            intent_raw[name] = _parse_datetime(intent_raw.get(name), name)
        intent_raw["action"] = DeploymentActionV107(intent_raw.get("action"))
        intent = DeploymentExecutionIntentV107(**intent_raw)
        approvals_raw = document.get("approvals")
        if not isinstance(approvals_raw, list):
            raise PostgreSQLRepositoryErrorV107("stored approvals must be a list")
        approvals: list[ApprovalAttestationV107] = []
        for raw in approvals_raw:
            if not isinstance(raw, dict):
                raise PostgreSQLRepositoryErrorV107("stored approval has invalid shape")
            item = dict(raw)
            item["role"] = ApprovalRoleV107(item.get("role"))
            item["signed_at"] = _parse_datetime(item.get("signed_at"), "signed_at")
            approvals.append(ApprovalAttestationV107(**item))
        return SignedDeploymentExecutionCommandV107(
            intent=intent,
            approvals=tuple(approvals),
            controller_key_id=document["controller_key_id"],
            controller_signature=document["controller_signature"],
        )
    except PostgreSQLRepositoryErrorV107:
        raise
    except (KeyError, ValueError, TypeError, ValidationErrorV107) as exc:
        raise PostgreSQLRepositoryErrorV107("stored command failed validation") from exc


@dataclass(frozen=True, slots=True)
class ClaimedExecutionV107:
    command: SignedDeploymentExecutionCommandV107
    state: ExecutionStateV107
    claimed_by: str
    mutation_attempts: int
    patch_digest: str | None
    pre_snapshot_digest: str | None


@dataclass(slots=True)
class PostgreSQLRolloutRepositoryV107:
    connection_factory: ConnectionFactoryV107

    @contextmanager
    def _transaction(self) -> Iterator[CursorV107]:
        connection = self.connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute("BEGIN")
            yield cursor
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            try:
                cursor.close()
            finally:
                connection.close()

    @staticmethod
    def _require_one(cursor: CursorV107, operation: str) -> None:
        if cursor.rowcount != 1:
            raise PostgreSQLConflictV107(f"{operation} affected {cursor.rowcount} rows")

    def enqueue(self, command: SignedDeploymentExecutionCommandV107, observed_at: datetime) -> None:
        current = _ensure_utc(observed_at)
        payload = command_to_json_v107(command)
        intent = command.intent
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO astra_rollout_replay_v107
                    (command_id, nonce, idempotency_key, consumed_at)
                VALUES (%s, %s, %s, %s)
                """,
                (intent.command_id, intent.nonce, intent.idempotency_key, current),
            )
            cursor.execute(
                """
                INSERT INTO astra_rollout_execution_v107
                    (command_id, action_id, command_digest, command_json, state,
                     deployment_uid, fencing_token, target_replicas, created_at, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, 'PENDING', %s, %s, %s, %s, %s)
                """,
                (
                    intent.command_id,
                    intent.action_id,
                    command.command_digest,
                    payload,
                    intent.deployment_uid,
                    intent.fencing_token,
                    intent.target_replicas,
                    current,
                    current,
                ),
            )
            cursor.execute(
                """
                INSERT INTO astra_rollout_outbox_v107
                    (event_id, command_id, event_type, payload_digest, created_at)
                VALUES (%s, %s, 'COMMAND_ENQUEUED', %s, %s)
                """,
                (
                    f"enqueue:{intent.command_id}",
                    intent.command_id,
                    digest_v107({"command": command.command_digest, "state": "PENDING"}),
                    current,
                ),
            )

    def claim_next(self, *, worker_id: str, observed_at: datetime) -> ClaimedExecutionV107 | None:
        current = _ensure_utc(observed_at)
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT command_id, command_json::text, state, mutation_attempts,
                       patch_digest, pre_snapshot_digest
                  FROM astra_rollout_execution_v107
                 WHERE state = 'PENDING'
                 ORDER BY created_at, command_id
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
                """
            )
            row = cursor.fetchone()
            if row is None:
                return None
            command_id, command_json, state, mutation_attempts, patch_digest, pre_snapshot_digest = row
            cursor.execute(
                """
                UPDATE astra_rollout_execution_v107
                   SET state = 'CLAIMED', claimed_by = %s, claimed_at = %s, updated_at = %s
                 WHERE command_id = %s AND state = 'PENDING'
                """,
                (worker_id, current, current, command_id),
            )
            self._require_one(cursor, "claim_next")
            return ClaimedExecutionV107(
                command=command_from_json_v107(command_json),
                state=ExecutionStateV107.CLAIMED,
                claimed_by=worker_id,
                mutation_attempts=int(mutation_attempts),
                patch_digest=patch_digest,
                pre_snapshot_digest=pre_snapshot_digest,
            )

    def load(self, command_id: str) -> ClaimedExecutionV107:
        connection = self.connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT command_json::text, state, claimed_by, mutation_attempts,
                       patch_digest, pre_snapshot_digest
                  FROM astra_rollout_execution_v107
                 WHERE command_id = %s
                """,
                (command_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise PostgreSQLNotFoundV107("rollout command not found")
            command_json, state, claimed_by, mutation_attempts, patch_digest, pre_snapshot_digest = row
            if claimed_by is None:
                raise PostgreSQLConflictV107("rollout command is not claimed")
            return ClaimedExecutionV107(
                command=command_from_json_v107(command_json),
                state=ExecutionStateV107(state),
                claimed_by=claimed_by,
                mutation_attempts=int(mutation_attempts),
                patch_digest=patch_digest,
                pre_snapshot_digest=pre_snapshot_digest,
            )
        finally:
            try:
                cursor.close()
            finally:
                connection.close()

    def record_preflight(
        self,
        *,
        command_id: str,
        worker_id: str,
        passed: bool,
        gates_digest: str,
        pre_snapshot_digest: str,
        observed_at: datetime,
    ) -> None:
        current = _ensure_utc(observed_at)
        state = "PREFLIGHT" if passed else "QUARANTINED"
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE astra_rollout_execution_v107
                   SET state = %s, preflight_digest = %s, pre_snapshot_digest = %s,
                       updated_at = %s
                 WHERE command_id = %s AND state = 'CLAIMED' AND claimed_by = %s
                """,
                (state, gates_digest, pre_snapshot_digest, current, command_id, worker_id),
            )
            self._require_one(cursor, "record_preflight")

    def mark_mutation_started(
        self,
        *,
        command_id: str,
        worker_id: str,
        deployment_uid: str,
        fencing_token: int,
        patch_digest: str,
        observed_at: datetime,
    ) -> None:
        current = _ensure_utc(observed_at)
        if fencing_token <= 0:
            raise ValidationErrorV107("fencing_token must be positive")
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO astra_rollout_fence_v107
                    (deployment_uid, fencing_token, command_id, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (deployment_uid) DO UPDATE
                   SET fencing_token = EXCLUDED.fencing_token,
                       command_id = EXCLUDED.command_id,
                       updated_at = EXCLUDED.updated_at
                 WHERE astra_rollout_fence_v107.fencing_token < EXCLUDED.fencing_token
                RETURNING fencing_token
                """,
                (deployment_uid, fencing_token, command_id, current),
            )
            if cursor.fetchone() is None:
                raise PostgreSQLConflictV107("fencing token is not newer than durable deployment fence")
            cursor.execute(
                """
                UPDATE astra_rollout_execution_v107
                   SET state = 'MUTATION_STARTED', mutation_attempts = 1,
                       patch_digest = %s, mutation_started_at = %s, updated_at = %s
                 WHERE command_id = %s AND state = 'PREFLIGHT'
                   AND claimed_by = %s AND mutation_attempts = 0
                """,
                (patch_digest, current, current, command_id, worker_id),
            )
            self._require_one(cursor, "mark_mutation_started")

    def mark_verifying(self, *, command_id: str, worker_id: str, observed_at: datetime) -> None:
        current = _ensure_utc(observed_at)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE astra_rollout_execution_v107
                   SET state = 'VERIFYING', updated_at = %s
                 WHERE command_id = %s AND state IN ('PREFLIGHT', 'MUTATION_STARTED', 'UNCERTAIN')
                   AND (claimed_by = %s OR recovery_by = %s)
                """,
                (current, command_id, worker_id, worker_id),
            )
            self._require_one(cursor, "mark_verifying")

    def mark_uncertain(self, *, command_id: str, worker_id: str, reason: str, observed_at: datetime) -> None:
        current = _ensure_utc(observed_at)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE astra_rollout_execution_v107
                   SET state = 'UNCERTAIN', failure_reason = %s, updated_at = %s
                 WHERE command_id = %s AND state IN ('MUTATION_STARTED', 'VERIFYING')
                   AND (claimed_by = %s OR recovery_by = %s)
                """,
                (reason, current, command_id, worker_id, worker_id),
            )
            self._require_one(cursor, "mark_uncertain")

    def complete(
        self,
        *,
        command_id: str,
        worker_id: str,
        receipt: ExecutionReceiptV107,
        observed_at: datetime,
    ) -> None:
        current = _ensure_utc(observed_at)
        receipt_json = json.dumps(asdict(receipt), sort_keys=True, separators=(",", ":"), default=_json_default)
        target_state = (
            "SUCCEEDED"
            if receipt.status in {ReceiptStatusV107.APPLIED, ReceiptStatusV107.ALREADY_APPLIED, ReceiptStatusV107.RECONCILED}
            else "UNCERTAIN"
            if receipt.status == ReceiptStatusV107.UNCERTAIN
            else "FAILED"
        )
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE astra_rollout_execution_v107
                   SET state = %s, receipt_digest = %s, receipt_json = %s::jsonb,
                       completed_at = %s, updated_at = %s
                 WHERE command_id = %s
                   AND state IN ('PREFLIGHT', 'VERIFYING', 'QUARANTINED', 'UNCERTAIN')
                   AND (claimed_by = %s OR recovery_by = %s)
                   AND mutation_attempts = %s
                """,
                (
                    target_state,
                    receipt.receipt_digest,
                    receipt_json,
                    current,
                    current,
                    command_id,
                    worker_id,
                    worker_id,
                    1 if receipt.mutation_attempted else 0,
                ),
            )
            self._require_one(cursor, "complete")
            cursor.execute(
                """
                INSERT INTO astra_rollout_event_v107
                    (command_id, event_type, observed_at, payload_digest)
                VALUES (%s, 'EXECUTION_COMPLETED', %s, %s)
                """,
                (command_id, current, receipt.receipt_digest),
            )

    def claim_recovery(
        self,
        *,
        command_id: str,
        worker_id: str,
        observed_at: datetime,
        claim_ttl_seconds: int,
    ) -> ClaimedExecutionV107:
        current = _ensure_utc(observed_at)
        if claim_ttl_seconds <= 0:
            raise ValidationErrorV107("claim_ttl_seconds must be positive")
        expired_before = current - timedelta(seconds=claim_ttl_seconds)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE astra_rollout_execution_v107
                   SET recovery_by = %s, recovery_claimed_at = %s, updated_at = %s
                 WHERE command_id = %s
                   AND state IN ('MUTATION_STARTED', 'VERIFYING', 'UNCERTAIN')
                   AND (state = 'UNCERTAIN' OR updated_at < %s)
                   AND (
                        recovery_by IS NULL
                        OR recovery_by = %s
                        OR recovery_claimed_at < %s
                   )
                RETURNING command_json::text, state, mutation_attempts,
                          patch_digest, pre_snapshot_digest
                """,
                (worker_id, current, current, command_id, expired_before, worker_id, expired_before),
            )
            row = cursor.fetchone()
            if row is None:
                raise PostgreSQLConflictV107("recovery claim rejected")
            command_json, state, mutation_attempts, patch_digest, pre_snapshot_digest = row
            return ClaimedExecutionV107(
                command=command_from_json_v107(command_json),
                state=ExecutionStateV107(state),
                claimed_by=worker_id,
                mutation_attempts=int(mutation_attempts),
                patch_digest=patch_digest,
                pre_snapshot_digest=pre_snapshot_digest,
            )

    def list_recoverable(self, *, limit: int = 100) -> tuple[str, ...]:
        if not (1 <= limit <= 1_000):
            raise ValidationErrorV107("limit outside allowed range")
        connection = self.connection_factory()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT command_id
                  FROM astra_rollout_execution_v107
                 WHERE state IN ('MUTATION_STARTED', 'VERIFYING', 'UNCERTAIN')
                 ORDER BY updated_at, command_id
                 LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            return tuple(row[0] for row in rows)
        finally:
            try:
                cursor.close()
            finally:
                connection.close()


POSTGRESQL_MUTATION_ATTEMPTS_V107 = 1
POSTGRESQL_RECOVERY_STATES_V107 = ("MUTATION_STARTED", "VERIFYING", "UNCERTAIN")
