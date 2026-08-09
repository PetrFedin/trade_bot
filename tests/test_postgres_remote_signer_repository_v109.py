from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta

import pytest

from app.runtime.postgres_remote_signer_repository_v109 import PostgresRemoteSignerRepositoryV109
from app.runtime.remote_signer_attestation_v109 import (
    ProviderAttestationV109,
    RemoteSignerAuditCheckpointV109,
    RemoteSignerConflictV109,
    RemoteSignerPolicySnapshotV109,
    RemoteSignRequestV109,
    VerifiedRemoteSignerPolicyV109,
    VerifiedRemoteSignResultV109,
    bytes_digest_v109,
)

UTC = UTC
NOW = datetime(2026, 8, 8, 20, 0, tzinfo=UTC)
DSN = os.getenv("ASTRA_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ASTRA_TEST_POSTGRES_DSN is required")


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def policy() -> VerifiedRemoteSignerPolicyV109:
    snapshot = RemoteSignerPolicySnapshotV109(
        provider_id="provider-pg",
        generation=1,
        endpoint_origin="https://signer.example.test",
        mtls_identity_ref="identity-pg",
        signing_key_id="signing-pg",
        signing_public_key_b64=b64(b"s" * 32),
        attestation_key_id="attest-pg",
        attestation_public_key_b64=b64(b"a" * 32),
        allowed_hardware_clusters=("cluster-pg",),
        allowed_firmware_measurements=("2" * 64,),
        predecessor_keyring_digest="1" * 64,
        request_ttl_seconds=60,
        timeout_seconds=2.0,
        max_response_bytes=8192,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        root_key_id="root-pg",
        root_signature_b64=b64(b"r" * 64),
    )
    return VerifiedRemoteSignerPolicyV109(snapshot=snapshot, verified_at=NOW)


def request(current: VerifiedRemoteSignerPolicyV109, *, suffix: str = "1") -> RemoteSignRequestV109:
    payload = f"payload-{suffix}".encode()
    return RemoteSignRequestV109(
        request_id=f"request-pg-{suffix}",
        nonce=f"nonce-pg-{suffix}",
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


def factory():
    import psycopg

    assert DSN is not None
    return psycopg.connect(DSN)


def clean() -> None:
    connection = factory()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE astra_remote_sign_event_v109,
                         astra_remote_sign_outbox_v109,
                         astra_remote_sign_request_v109,
                         astra_remote_sign_checkpoint_v109,
                         astra_remote_sign_policy_v109
                RESTART IDENTITY CASCADE
                """
            )
        connection.commit()
    finally:
        connection.close()


def attestation(
    current: VerifiedRemoteSignerPolicyV109, item: RemoteSignRequestV109
) -> ProviderAttestationV109:
    return ProviderAttestationV109(
        request_id=item.request_id,
        request_digest=item.request_digest,
        signature_digest=bytes_digest_v109(b"x" * 64),
        provider_id=current.provider_id,
        policy_generation=current.generation,
        policy_digest=current.policy_digest,
        signing_key_id=current.snapshot.signing_key_id,
        attestation_key_id=current.snapshot.attestation_key_id,
        hardware_cluster_id="cluster-pg",
        firmware_measurement="2" * 64,
        hardware_signing_counter=1,
        audit_sequence=1,
        audit_event_digest="3" * 64,
        audit_chain_root="4" * 64,
        attested_at=NOW + timedelta(seconds=1),
        attestation_signature_b64=b64(b"z" * 64),
    )


def test_postgres_request_outbox_uncertainty_and_signed_checkpoint() -> None:
    clean()
    current = policy()
    repo = PostgresRemoteSignerRepositoryV109(factory)
    repo.install_verified_policy(current)
    repo.install_verified_policy(current)
    item = request(current)
    repo.create_request_with_outbox(item, b"payload-1")
    assert repo.load_request(item.request_id).request_digest == item.request_digest
    assert repo.load_checkpoint(current.provider_id) is None
    repo.mark_dispatch_started(item.request_id, worker_id="worker-pg", observed_at=NOW)
    repo.record_uncertain(
        item.request_id, reason="lost response", observed_at=NOW + timedelta(seconds=1)
    )
    evidence = attestation(current, item)
    checkpoint = RemoteSignerAuditCheckpointV109(
        provider_id=current.provider_id,
        policy_generation=current.generation,
        audit_sequence=1,
        hardware_signing_counter=1,
        audit_chain_root="4" * 64,
        observed_at=NOW + timedelta(seconds=2),
    )
    result = VerifiedRemoteSignResultV109(
        signature=b"x" * 64, attestation=evidence, checkpoint=checkpoint
    )
    repo.record_signed(item, result, observed_at=NOW + timedelta(seconds=2))
    stored = repo.load_checkpoint(current.provider_id)
    assert stored is not None
    assert stored.audit_sequence == 1


def test_postgres_policy_equivocation_and_dispatch_replay_fail_closed() -> None:
    clean()
    current = policy()
    repo = PostgresRemoteSignerRepositoryV109(factory)
    repo.install_verified_policy(current)
    changed = policy()
    object.__setattr__(changed.snapshot, "endpoint_origin", "https://other.example.test")
    with pytest.raises(RemoteSignerConflictV109, match="equivocation"):
        repo.install_verified_policy(changed)
    item = request(current, suffix="2")
    repo.create_request_with_outbox(item, b"payload-2")
    repo.mark_dispatch_started(item.request_id, worker_id="worker-pg", observed_at=NOW)
    with pytest.raises(RemoteSignerConflictV109, match="dispatch"):
        repo.mark_dispatch_started(item.request_id, worker_id="worker-pg", observed_at=NOW)
    repo.record_rejected(
        item.request_id, reason="policy rejection", observed_at=NOW + timedelta(seconds=1)
    )
    with pytest.raises(RemoteSignerConflictV109, match="transition"):
        repo.record_uncertain(
            item.request_id, reason="late update", observed_at=NOW + timedelta(seconds=2)
        )


def test_postgres_audit_event_is_append_only() -> None:
    clean()
    current = policy()
    repo = PostgresRemoteSignerRepositoryV109(factory)
    repo.install_verified_policy(current)
    connection = factory()
    try:
        with connection.cursor() as cursor:
            with pytest.raises(Exception, match="append-only"):
                cursor.execute("UPDATE astra_remote_sign_event_v109 SET event_type = 'TAMPERED'")
        connection.rollback()
    finally:
        connection.close()
