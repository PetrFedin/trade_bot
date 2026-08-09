from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import app.runtime.postgres_remote_signer_repository_v109 as pg
from app.runtime.postgres_remote_signer_repository_v109 import (
    PostgreSQLRemoteSignerRepositoryErrorV109,
    PostgresRemoteSignerRepositoryV109,
)
from app.runtime.remote_signer_attestation_v109 import (
    ProviderAttestationV109,
    RemoteSignerAuditCheckpointV109,
    RemoteSignerConflictV109,
    RemoteSignerPolicySnapshotV109,
    RemoteSignerValidationErrorV109,
    RemoteSignRequestV109,
    VerifiedRemoteSignerPolicyV109,
    VerifiedRemoteSignResultV109,
    bytes_digest_v109,
)

UTC = UTC
NOW = datetime(2026, 8, 8, 20, 0, tzinfo=UTC)


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def policy() -> VerifiedRemoteSignerPolicyV109:
    snapshot = RemoteSignerPolicySnapshotV109(
        provider_id="provider-unit",
        generation=1,
        endpoint_origin="https://signer.example.test",
        mtls_identity_ref="identity-unit",
        signing_key_id="signing-unit",
        signing_public_key_b64=b64(b"s" * 32),
        attestation_key_id="attest-unit",
        attestation_public_key_b64=b64(b"a" * 32),
        allowed_hardware_clusters=("cluster-unit",),
        allowed_firmware_measurements=("2" * 64,),
        predecessor_keyring_digest="1" * 64,
        request_ttl_seconds=60,
        timeout_seconds=2.0,
        max_response_bytes=8192,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        root_key_id="root-unit",
        root_signature_b64=b64(b"r" * 64),
    )
    return VerifiedRemoteSignerPolicyV109(snapshot=snapshot, verified_at=NOW)


def request(current: VerifiedRemoteSignerPolicyV109, *, suffix: str = "1") -> RemoteSignRequestV109:
    payload = f"payload-{suffix}".encode()
    return RemoteSignRequestV109(
        request_id=f"request-unit-{suffix}",
        nonce=f"nonce-unit-{suffix}",
        provider_id=current.provider_id,
        policy_generation=current.generation,
        policy_digest=current.policy_digest,
        key_id=current.snapshot.signing_key_id,
        key_generation=1,
        keyring_generation=1,
        purpose="CONTROLLER_COMMAND",
        domain="astra.controller.command.v109",
        payload_digest=bytes_digest_v109(payload),
        created_at=NOW,
        deadline_at=NOW + timedelta(seconds=60),
    )


def result(
    current: VerifiedRemoteSignerPolicyV109, item: RemoteSignRequestV109
) -> VerifiedRemoteSignResultV109:
    signature = b"x" * 64
    evidence = ProviderAttestationV109(
        request_id=item.request_id,
        request_digest=item.request_digest,
        signature_digest=bytes_digest_v109(signature),
        provider_id=current.provider_id,
        policy_generation=current.generation,
        policy_digest=current.policy_digest,
        signing_key_id=current.snapshot.signing_key_id,
        attestation_key_id=current.snapshot.attestation_key_id,
        hardware_cluster_id="cluster-unit",
        firmware_measurement="2" * 64,
        hardware_signing_counter=2,
        audit_sequence=3,
        audit_event_digest="3" * 64,
        audit_chain_root="4" * 64,
        attested_at=NOW + timedelta(seconds=1),
        attestation_signature_b64=b64(b"z" * 64),
    )
    checkpoint = RemoteSignerAuditCheckpointV109(
        provider_id=current.provider_id,
        policy_generation=current.generation,
        audit_sequence=3,
        hardware_signing_counter=2,
        audit_chain_root="4" * 64,
        observed_at=NOW + timedelta(seconds=2),
    )
    return VerifiedRemoteSignResultV109(
        signature=signature, attestation=evidence, checkpoint=checkpoint
    )


class FakeCursor:
    def __init__(self, fetches: list[Any] | None = None, *, fail_on: str | None = None) -> None:
        self.fetches = list(fetches or [])
        self.fail_on = fail_on
        self.queries: list[tuple[str, Any]] = []
        self.closed = False
        self.rowcount = 1

    def execute(self, query: str, params: Any = None) -> None:
        normalized = " ".join(query.split())
        self.queries.append((normalized, params))
        if self.fail_on and self.fail_on in normalized:
            raise RuntimeError("forced database failure")

    def fetchone(self) -> Any:
        return self.fetches.pop(0) if self.fetches else None

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def repo_with(
    fetches: list[Any] | None = None, *, fail_on: str | None = None
) -> tuple[PostgresRemoteSignerRepositoryV109, FakeConnection]:
    connection = FakeConnection(FakeCursor(fetches, fail_on=fail_on))
    return PostgresRemoteSignerRepositoryV109(lambda: connection), connection


def test_postgres_helpers_and_transaction_lifecycle() -> None:
    with pytest.raises(RemoteSignerValidationErrorV109, match="timezone-aware"):
        pg._ensure_utc(datetime(2026, 8, 8, 20, 0))
    assert json.loads(pg._json({"b": 2, "a": 1})) == {"a": 1, "b": 2}
    assert pg._row_value({"value": "mapping"}, 0, "value") == "mapping"
    assert pg._row_value(("tuple",), 0, "value") == "tuple"

    current = policy()
    repository, connection = repo_with(fail_on="INSERT INTO astra_remote_sign_policy_v109")
    with pytest.raises(RuntimeError, match="forced"):
        repository.install_verified_policy(current)
    assert connection.rollbacks == 1
    assert connection.commits == 0
    assert connection._cursor.closed is True
    assert connection.closed is True


def test_policy_install_idempotence_and_equivocation_paths() -> None:
    current = policy()
    repository, connection = repo_with([(current.policy_digest,)])
    repository.install_verified_policy(current)
    assert connection.commits == 1
    assert any("POLICY_ACCEPTED" in query for query, _ in connection._cursor.queries)

    repository, connection = repo_with([None, {"snapshot_digest": current.policy_digest}])
    repository.install_verified_policy(current)
    assert connection.commits == 1

    repository, connection = repo_with([None, {"snapshot_digest": "0" * 64}])
    with pytest.raises(RemoteSignerConflictV109, match="equivocation"):
        repository.install_verified_policy(current)
    assert connection.rollbacks == 1


def test_create_dispatch_and_transition_paths() -> None:
    current = policy()
    item = request(current)
    repository, connection = repo_with()
    with pytest.raises(RemoteSignerValidationErrorV109, match="non-empty"):
        repository.create_request_with_outbox(item, b"")
    repository.create_request_with_outbox(item, b"payload-1")
    assert connection.commits == 1
    assert any("astra_remote_sign_outbox_v109" in query for query, _ in connection._cursor.queries)

    repository, connection = repo_with([{"request_digest": item.request_digest}])
    repository.mark_dispatch_started(item.request_id, worker_id="worker-unit", observed_at=NOW)
    assert connection.commits == 1
    assert any("DISPATCH_STARTED" in query for query, _ in connection._cursor.queries)

    repository, connection = repo_with([None])
    with pytest.raises(RemoteSignerConflictV109, match="dispatch"):
        repository.mark_dispatch_started(item.request_id, worker_id="worker-unit", observed_at=NOW)
    assert connection.rollbacks == 1

    for method_name in ("record_rejected", "record_uncertain", "record_quarantined"):
        repository, connection = repo_with([(item.request_digest,)])
        getattr(repository, method_name)(item.request_id, reason="reason", observed_at=NOW)
        assert connection.commits == 1

    repository, connection = repo_with([None])
    with pytest.raises(RemoteSignerConflictV109, match="transition"):
        repository.record_uncertain(item.request_id, reason="late", observed_at=NOW)
    assert connection.rollbacks == 1


def test_load_request_and_checkpoint_row_shapes() -> None:
    current = policy()
    item = request(current)
    serialized = json.dumps(
        item.to_payload(), default=lambda value: value.isoformat().replace("+00:00", "Z")
    )
    repository, _ = repo_with([(serialized,)])
    assert repository.load_request(item.request_id).request_digest == item.request_digest

    repository, _ = repo_with([{"request_json": json.loads(serialized)}])
    assert repository.load_request(item.request_id).request_digest == item.request_digest

    repository, connection = repo_with([None])
    with pytest.raises(KeyError):
        repository.load_request(item.request_id)
    assert connection.rollbacks == 1

    repository, connection = repo_with([(123,)])
    with pytest.raises(PostgreSQLRemoteSignerRepositoryErrorV109, match="request JSON"):
        repository.load_request(item.request_id)
    assert connection.rollbacks == 1

    repository, _ = repo_with([None])
    assert repository.load_checkpoint(current.provider_id) is None

    row = {
        "provider_id": current.provider_id,
        "policy_generation": 1,
        "audit_sequence": 3,
        "hardware_signing_counter": 2,
        "audit_chain_root": "4" * 64,
        "observed_at": NOW,
    }
    repository, _ = repo_with([row])
    checkpoint = repository.load_checkpoint(current.provider_id)
    assert checkpoint is not None
    assert checkpoint.audit_sequence == 3


def test_record_signed_compare_and_set_and_transition_paths() -> None:
    current = policy()
    item = request(current)
    signed = result(current, item)

    repository, connection = repo_with([(3,), (item.request_id,)])
    repository.record_signed(item, signed, observed_at=NOW + timedelta(seconds=2))
    assert connection.commits == 1
    assert any("'SIGNED'" in query for query, _ in connection._cursor.queries)

    repository, connection = repo_with([None])
    with pytest.raises(RemoteSignerConflictV109, match="compare-and-set"):
        repository.record_signed(item, signed, observed_at=NOW + timedelta(seconds=2))
    assert connection.rollbacks == 1

    repository, connection = repo_with([(3,), None])
    with pytest.raises(RemoteSignerConflictV109, match="signed request transition"):
        repository.record_signed(item, signed, observed_at=NOW + timedelta(seconds=2))
    assert connection.rollbacks == 1
