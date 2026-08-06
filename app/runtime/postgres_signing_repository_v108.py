from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from app.runtime.signing_authority_v108 import (
    RootSignedKeyringSnapshotV108,
    ReceiptAuthorizationV108,
    RolloutAuthorizationBundleV108,
    SignatureEnvelopeV108,
    SigningAuthorityErrorV108,
    canonical_bytes_v108,
)

UTC = timezone.utc


class PostgreSQLSigningRepositoryErrorV108(SigningAuthorityErrorV108):
    pass


class PostgreSQLSigningConflictV108(PostgreSQLSigningRepositoryErrorV108):
    pass


class CursorV108(Protocol):
    rowcount: int

    def execute(self, query: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> Any: ...

    def fetchone(self) -> Any: ...

    def close(self) -> Any: ...


class ConnectionV108(Protocol):
    def cursor(self) -> CursorV108: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...

    def close(self) -> Any: ...


ConnectionFactoryV108 = Callable[[], ConnectionV108]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PostgreSQLSigningRepositoryErrorV108("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _json(value: Any) -> str:
    return canonical_bytes_v108(value).decode("utf-8")


@dataclass(slots=True)
class PostgreSQLSigningRepositoryV108:
    connection_factory: ConnectionFactoryV108

    @contextmanager
    def _transaction(self) -> Iterator[CursorV108]:
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

    def persist_keyring_snapshot(
        self, snapshot: RootSignedKeyringSnapshotV108, *, observed_at: datetime
    ) -> None:
        current = _ensure_utc(observed_at)
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO astra_signing_keyring_v108
                    (singleton, generation, snapshot_digest, snapshot_json,
                     root_key_id, root_signature_b64, issued_at, expires_at, updated_at)
                VALUES (TRUE, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                ON CONFLICT (singleton) DO UPDATE
                   SET generation = EXCLUDED.generation,
                       snapshot_digest = EXCLUDED.snapshot_digest,
                       snapshot_json = EXCLUDED.snapshot_json,
                       root_key_id = EXCLUDED.root_key_id,
                       root_signature_b64 = EXCLUDED.root_signature_b64,
                       issued_at = EXCLUDED.issued_at,
                       expires_at = EXCLUDED.expires_at,
                       updated_at = EXCLUDED.updated_at
                 WHERE astra_signing_keyring_v108.generation < EXCLUDED.generation
                RETURNING generation
                """,
                (
                    snapshot.generation,
                    snapshot.snapshot_digest,
                    _json(snapshot.to_payload()),
                    snapshot.root_key_id,
                    snapshot.root_signature_b64,
                    snapshot.issued_at,
                    snapshot.expires_at,
                    current,
                ),
            )
            if cursor.fetchone() is None:
                raise PostgreSQLSigningConflictV108("keyring generation is not newer")
            cursor.execute(
                """
                INSERT INTO astra_signing_event_v108
                    (event_type, subject_id, observed_at, payload_digest)
                VALUES ('KEYRING_ACCEPTED', %s, %s, %s)
                """,
                (str(snapshot.generation), current, snapshot.snapshot_digest),
            )

    def reserve_authorization_bundle(
        self, bundle: RolloutAuthorizationBundleV108, *, observed_at: datetime
    ) -> None:
        current = _ensure_utc(observed_at)
        envelopes: tuple[SignatureEnvelopeV108, ...] = (
            bundle.release,
            bundle.risk,
            bundle.controller,
        )
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO astra_rollout_authorization_v108
                    (bundle_id, command_digest, policy_digest,
                     predecessor_release_identity_digest, authorization_digest,
                     bundle_digest, bundle_json, keyring_generation, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    bundle.bundle_id,
                    bundle.command_digest,
                    bundle.policy_digest,
                    bundle.predecessor_release_identity_digest,
                    bundle.authorization_digest,
                    bundle.bundle_digest,
                    _json(bundle.to_payload()),
                    bundle.keyring_generation,
                    current,
                ),
            )
            for envelope in envelopes:
                cursor.execute(
                    """
                    INSERT INTO astra_signature_replay_v108
                        (signature_id, nonce, purpose, domain, payload_digest,
                         key_id, key_generation, keyring_generation, consumed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        envelope.signature_id,
                        envelope.nonce,
                        envelope.purpose.value,
                        envelope.domain,
                        envelope.payload_digest,
                        envelope.key_id,
                        envelope.key_generation,
                        envelope.keyring_generation,
                        current,
                    ),
                )
            cursor.execute(
                """
                INSERT INTO astra_signing_event_v108
                    (event_type, subject_id, observed_at, payload_digest)
                VALUES ('ROLLOUT_AUTHORIZATION_RESERVED', %s, %s, %s)
                """,
                (bundle.bundle_id, current, bundle.bundle_digest),
            )

    def reserve_receipt_authorization(
        self, receipt: ReceiptAuthorizationV108, *, observed_at: datetime
    ) -> None:
        current = _ensure_utc(observed_at)
        envelope = receipt.executor
        with self._transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO astra_signature_replay_v108
                    (signature_id, nonce, purpose, domain, payload_digest,
                     key_id, key_generation, keyring_generation, consumed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    envelope.signature_id,
                    envelope.nonce,
                    envelope.purpose.value,
                    envelope.domain,
                    envelope.payload_digest,
                    envelope.key_id,
                    envelope.key_generation,
                    envelope.keyring_generation,
                    current,
                ),
            )
            cursor.execute(
                """
                INSERT INTO astra_receipt_authorization_v108
                    (receipt_id, receipt_digest, command_digest,
                     authorization_bundle_digest, payload_digest, authorization_digest,
                     executor_signature_id, receipt_json, keyring_generation, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    receipt.receipt_id,
                    receipt.receipt_digest,
                    receipt.command_digest,
                    receipt.authorization_bundle_digest,
                    receipt.payload_digest,
                    receipt.authorization_digest,
                    envelope.signature_id,
                    _json(receipt.to_payload()),
                    receipt.keyring_generation,
                    current,
                ),
            )
            cursor.execute(
                """
                INSERT INTO astra_signing_event_v108
                    (event_type, subject_id, observed_at, payload_digest)
                VALUES ('RECEIPT_AUTHORIZATION_RESERVED', %s, %s, %s)
                """,
                (receipt.receipt_id, current, receipt.authorization_digest),
            )
