from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.runtime.remote_signer_attestation_v109 import (
    ProviderAttestationV109,
    RemoteSignerAuditCheckpointV109,
    RemoteSignerConflictV109,
    RemoteSignerErrorV109,
    RemoteSignerValidationErrorV109,
    RemoteSignRequestV109,
    VerifiedRemoteSignerPolicyV109,
    VerifiedRemoteSignResultV109,
    canonical_bytes_v109,
)

UTC = UTC


class PostgreSQLRemoteSignerRepositoryErrorV109(RemoteSignerErrorV109):
    pass


class CursorV109(Protocol):
    rowcount: int

    def execute(
        self, query: str, params: Sequence[Any] | Mapping[str, Any] | None = None
    ) -> Any: ...

    def fetchone(self) -> Any: ...

    def close(self) -> Any: ...


class ConnectionV109(Protocol):
    def cursor(self) -> CursorV109: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...

    def close(self) -> Any: ...


ConnectionFactoryV109 = Callable[[], ConnectionV109]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RemoteSignerValidationErrorV109("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _json(value: Any) -> str:
    return canonical_bytes_v109(value).decode("utf-8")


def _row_value(row: Any, index: int, key: str) -> Any:
    if isinstance(row, Mapping):
        return row[key]
    return row[index]


@dataclass(slots=True)
class PostgresRemoteSignerRepositoryV109:
    connection_factory: ConnectionFactoryV109

    @contextmanager
    def _transaction(self) -> Iterator[CursorV109]:
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

    def install_verified_policy(self, policy: VerifiedRemoteSignerPolicyV109) -> None:
        snapshot = policy.snapshot
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO astra_remote_sign_policy_v109
                    (provider_id, generation, snapshot_digest, snapshot_json,
                     endpoint_origin, mtls_identity_ref, signing_key_id, attestation_key_id,
                     issued_at, expires_at, installed_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider_id, generation) DO NOTHING
                RETURNING snapshot_digest
                """,
                (
                    policy.provider_id,
                    policy.generation,
                    policy.policy_digest,
                    _json(snapshot.to_payload()),
                    snapshot.endpoint_origin,
                    snapshot.mtls_identity_ref,
                    snapshot.signing_key_id,
                    snapshot.attestation_key_id,
                    snapshot.issued_at,
                    snapshot.expires_at,
                    policy.verified_at,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    """
                    SELECT snapshot_digest
                      FROM astra_remote_sign_policy_v109
                     WHERE provider_id = %s AND generation = %s
                    """,
                    (policy.provider_id, policy.generation),
                )
                existing = cursor.fetchone()
                if (
                    existing is None
                    or _row_value(existing, 0, "snapshot_digest") != policy.policy_digest
                ):
                    raise RemoteSignerConflictV109("policy generation equivocation")
            cursor.execute(
                """
                INSERT INTO astra_remote_sign_event_v109
                    (request_id, event_type, observed_at, payload_digest, details_json)
                VALUES (NULL, 'POLICY_ACCEPTED', %s, %s, %s::jsonb)
                """,
                (
                    policy.verified_at,
                    policy.policy_digest,
                    _json({"provider_id": policy.provider_id, "generation": policy.generation}),
                ),
            )

    def create_request_with_outbox(self, request: RemoteSignRequestV109, payload: bytes) -> None:
        if not isinstance(payload, bytes) or not payload:
            raise RemoteSignerValidationErrorV109("payload must be non-empty bytes")
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO astra_remote_sign_request_v109
                    (request_id, nonce, provider_id, policy_generation, policy_digest,
                     request_digest, request_json, payload_digest, state, created_at,
                     deadline_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, 'CREATED', %s, %s, %s)
                """,
                (
                    request.request_id,
                    request.nonce,
                    request.provider_id,
                    request.policy_generation,
                    request.policy_digest,
                    request.request_digest,
                    _json(request.to_payload()),
                    request.payload_digest,
                    request.created_at,
                    request.deadline_at,
                    request.created_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO astra_remote_sign_outbox_v109
                    (request_id, payload_b64, created_at)
                VALUES (%s, %s, %s)
                """,
                (request.request_id, base64.b64encode(payload).decode("ascii"), request.created_at),
            )
            cursor.execute(
                """
                INSERT INTO astra_remote_sign_event_v109
                    (request_id, event_type, observed_at, payload_digest, details_json)
                VALUES (%s, 'REQUEST_CREATED', %s, %s, '{}'::jsonb)
                """,
                (request.request_id, request.created_at, request.request_digest),
            )

    def mark_dispatch_started(
        self, request_id: str, *, worker_id: str, observed_at: datetime
    ) -> None:
        current = _ensure_utc(observed_at)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE astra_remote_sign_request_v109
                   SET state = 'DISPATCH_STARTED', dispatch_worker_id = %s,
                       dispatch_started_at = %s, updated_at = %s
                 WHERE request_id = %s AND state = 'CREATED'
                RETURNING request_digest
                """,
                (worker_id, current, current, request_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise RemoteSignerConflictV109("dispatch claim rejected")
            cursor.execute(
                """
                UPDATE astra_remote_sign_outbox_v109
                   SET dispatched_at = %s
                 WHERE request_id = %s AND dispatched_at IS NULL
                """,
                (current, request_id),
            )
            cursor.execute(
                """
                INSERT INTO astra_remote_sign_event_v109
                    (request_id, event_type, observed_at, payload_digest, details_json)
                VALUES (%s, 'DISPATCH_STARTED', %s, %s, %s::jsonb)
                """,
                (
                    request_id,
                    current,
                    _row_value(row, 0, "request_digest"),
                    _json({"worker_id": worker_id}),
                ),
            )

    def _record_terminal_or_uncertain(
        self,
        request_id: str,
        *,
        state: str,
        event_type: str,
        reason: str,
        observed_at: datetime,
    ) -> None:
        current = _ensure_utc(observed_at)
        with self._transaction() as cursor:
            cursor.execute(
                """
                UPDATE astra_remote_sign_request_v109
                   SET state = %s, failure_reason = %s, updated_at = %s
                 WHERE request_id = %s
                   AND state IN ('DISPATCH_STARTED', 'UNCERTAIN')
                RETURNING request_digest
                """,
                (state, reason, current, request_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise RemoteSignerConflictV109("request transition rejected")
            cursor.execute(
                """
                INSERT INTO astra_remote_sign_event_v109
                    (request_id, event_type, observed_at, payload_digest, details_json)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (
                    request_id,
                    event_type,
                    current,
                    _row_value(row, 0, "request_digest"),
                    _json({"reason": reason}),
                ),
            )

    def record_rejected(self, request_id: str, *, reason: str, observed_at: datetime) -> None:
        self._record_terminal_or_uncertain(
            request_id,
            state="REJECTED",
            event_type="REJECTED",
            reason=reason,
            observed_at=observed_at,
        )

    def record_uncertain(self, request_id: str, *, reason: str, observed_at: datetime) -> None:
        self._record_terminal_or_uncertain(
            request_id,
            state="UNCERTAIN",
            event_type="UNCERTAIN",
            reason=reason,
            observed_at=observed_at,
        )

    def record_quarantined(self, request_id: str, *, reason: str, observed_at: datetime) -> None:
        self._record_terminal_or_uncertain(
            request_id,
            state="QUARANTINED",
            event_type="QUARANTINED",
            reason=reason,
            observed_at=observed_at,
        )

    def load_request(self, request_id: str) -> RemoteSignRequestV109:
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT request_json FROM astra_remote_sign_request_v109 WHERE request_id = %s",
                (request_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(request_id)
            raw = _row_value(row, 0, "request_json")
            if isinstance(raw, str):
                payload = json.loads(raw)
            elif isinstance(raw, Mapping):
                payload = dict(raw)
            else:
                raise PostgreSQLRemoteSignerRepositoryErrorV109("invalid request JSON")
            return RemoteSignRequestV109.from_payload(payload)

    def load_checkpoint(self, provider_id: str) -> RemoteSignerAuditCheckpointV109 | None:
        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT provider_id, policy_generation, audit_sequence,
                       hardware_signing_counter, audit_chain_root, observed_at
                  FROM astra_remote_sign_checkpoint_v109
                 WHERE provider_id = %s
                """,
                (provider_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return RemoteSignerAuditCheckpointV109(
                provider_id=str(_row_value(row, 0, "provider_id")),
                policy_generation=int(_row_value(row, 1, "policy_generation")),
                audit_sequence=int(_row_value(row, 2, "audit_sequence")),
                hardware_signing_counter=int(_row_value(row, 3, "hardware_signing_counter")),
                audit_chain_root=str(_row_value(row, 4, "audit_chain_root")),
                observed_at=_row_value(row, 5, "observed_at"),
            )

    def record_signed(
        self,
        request: RemoteSignRequestV109,
        result: VerifiedRemoteSignResultV109,
        *,
        observed_at: datetime,
    ) -> None:
        current = _ensure_utc(observed_at)
        attestation: ProviderAttestationV109 = result.attestation
        checkpoint = result.checkpoint
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO astra_remote_sign_checkpoint_v109 AS checkpoint
                    (provider_id, policy_generation, audit_sequence,
                     hardware_signing_counter, audit_chain_root, observed_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider_id) DO UPDATE
                   SET policy_generation = EXCLUDED.policy_generation,
                       audit_sequence = EXCLUDED.audit_sequence,
                       hardware_signing_counter = EXCLUDED.hardware_signing_counter,
                       audit_chain_root = EXCLUDED.audit_chain_root,
                       observed_at = EXCLUDED.observed_at
                 WHERE checkpoint.policy_generation <= EXCLUDED.policy_generation
                   AND checkpoint.audit_sequence < EXCLUDED.audit_sequence
                   AND checkpoint.hardware_signing_counter < EXCLUDED.hardware_signing_counter
                RETURNING audit_sequence
                """,
                (
                    checkpoint.provider_id,
                    checkpoint.policy_generation,
                    checkpoint.audit_sequence,
                    checkpoint.hardware_signing_counter,
                    checkpoint.audit_chain_root,
                    checkpoint.observed_at,
                ),
            )
            if cursor.fetchone() is None:
                raise RemoteSignerConflictV109("audit checkpoint compare-and-set rejected")
            cursor.execute(
                """
                UPDATE astra_remote_sign_request_v109
                   SET state = 'SIGNED', signature_b64 = %s,
                       attestation_json = %s::jsonb, failure_reason = NULL, updated_at = %s
                 WHERE request_id = %s
                   AND request_digest = %s
                   AND state IN ('DISPATCH_STARTED', 'UNCERTAIN')
                RETURNING request_id
                """,
                (
                    base64.b64encode(result.signature).decode("ascii"),
                    _json(attestation.to_payload()),
                    current,
                    request.request_id,
                    request.request_digest,
                ),
            )
            if cursor.fetchone() is None:
                raise RemoteSignerConflictV109("signed request transition rejected")
            cursor.execute(
                """
                INSERT INTO astra_remote_sign_event_v109
                    (request_id, event_type, observed_at, payload_digest, details_json)
                VALUES (%s, 'SIGNED', %s, %s, %s::jsonb)
                """,
                (
                    request.request_id,
                    current,
                    request.request_digest,
                    _json(
                        {
                            "audit_sequence": attestation.audit_sequence,
                            "hardware_signing_counter": attestation.hardware_signing_counter,
                            "audit_chain_root": attestation.audit_chain_root,
                        }
                    ),
                ),
            )
