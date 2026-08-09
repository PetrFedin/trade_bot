from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import threading

import pytest

from app.runtime.worker_execution_plane_v104 import (
    AuthorizationError,
    CapacityError,
    ClaimOutcome,
    DeadLetterQueueV104,
    DlqReason,
    EvidenceSpoolV104,
    HeartbeatGuardV104,
    HmacKeyRingV104,
    IntegrityError,
    PAPER_REST_BASE,
    PermanentTransportError,
    ReadOnlyAlpacaRunnerV104,
    ReplayError,
    ReplayLedgerV104,
    ResumableUploaderV104,
    SignedWorkClaimV104,
    SpoolRecordV104,
    StaleClaimError,
    TransientTransportError,
    WorkerAttestationV104,
    WorkerEventJournalV104,
    WorkerExecutionPlaneV104,
    WorkerHeartbeatV104,
    WorkerPlaneError,
    WorkerPolicyV104,
    WorkerState,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)
SECRET = b"x" * 32


class FakeTransport:
    def __init__(self, failures=0, permanent=False):
        self.failures = failures
        self.permanent = permanent
        self.calls = []

    def get(self, url, *, headers, timeout_seconds, tls_verify, allow_redirects):
        self.calls.append((url, dict(headers), timeout_seconds, tls_verify, allow_redirects))
        if self.permanent:
            raise PermanentTransportError("permanent")
        if self.failures:
            self.failures -= 1
            raise TransientTransportError("temporary")
        return {"url": url, "status": "ok", "secret": "hidden", "nested": [{"token": "x"}]}


class FakeObjectStore:
    def __init__(self):
        self.uploads = {}
        self.parts = {}
        self.create_calls = 0
        self.complete_calls = 0
        self.empty_etag = False
        self.bad_digest = False

    def create_upload(self, object_key, metadata):
        self.create_calls += 1
        upload_id = f"up-{self.create_calls}"
        self.uploads[upload_id] = (object_key, dict(metadata))
        return upload_id

    def upload_part(self, upload_id, part_number, data, digest):
        assert hashlib.sha256(data).hexdigest() == digest
        self.parts[(upload_id, part_number)] = bytes(data)
        return "" if self.empty_etag else digest

    def complete_upload(self, upload_id, parts, total_digest):
        self.complete_calls += 1
        combined = b"".join(self.parts[(upload_id, number)] for number, _ in parts)
        actual = hashlib.sha256(combined).hexdigest()
        return "0" * 64 if self.bad_digest else actual

    def abort_upload(self, upload_id):
        self.uploads.pop(upload_id, None)


def policy(**changes):
    values = dict(policy_id="p1", generation=7)
    values.update(changes)
    return WorkerPolicyV104(**values)


def signed_inputs(p, *, claim_changes=None, attestation_changes=None):
    ring = HmacKeyRingV104({"k1": SECRET})
    att = WorkerAttestationV104(
        worker_id="worker-1", deployment_id="deploy-1", image_digest="sha256:" + "a" * 64,
        source_commit="b" * 40, policy_digest=p.digest, generation=p.generation,
        created_at=NOW - timedelta(seconds=1), expires_at=NOW + timedelta(minutes=10), nonce="att-nonce", key_id="k1",
    )
    if attestation_changes:
        att = att.__class__(**{**{item.name: getattr(att, item.name) for item in fields(att)}, **attestation_changes})
    att = ring.sign_attestation(att)
    claim = SignedWorkClaimV104(
        claim_id="claim-1", campaign_id="campaign-1", run_id="run-1", generation=p.generation,
        fencing_token=3, endpoints=("account", "orders", "positions", "clock"),
        issued_at=NOW - timedelta(seconds=1), not_before=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=2), worker_id="worker-1", deployment_id="deploy-1",
        policy_digest=p.digest, nonce="claim-nonce", key_id="k1",
    )
    if claim_changes:
        payload = {field: getattr(claim, field) for field in claim.__dataclass_fields__}
        payload.update(claim_changes)
        claim = SignedWorkClaimV104(**payload)
    claim = ring.sign_claim(claim)
    return ring, claim, att


def build_plane(tmp_path, transport=None, store=None, p=None):
    p = p or policy()
    ring, claim, att = signed_inputs(p)
    transport = transport or FakeTransport()
    store = store or FakeObjectStore()
    spool = EvidenceSpoolV104(tmp_path / "spool", p.spool_max_files, p.spool_max_bytes)
    uploader = ResumableUploaderV104(tmp_path / "uploads", store, p.multipart_part_bytes, SECRET)
    dlq = DeadLetterQueueV104(tmp_path / "dlq.jsonl")
    journal = WorkerEventJournalV104(tmp_path / "events.jsonl")
    plane = WorkerExecutionPlaneV104(p, ring, ReplayLedgerV104(), ReadOnlyAlpacaRunnerV104(transport, "id", "secret"), spool, uploader, dlq, journal)
    return plane, claim, att, transport, store


@pytest.mark.parametrize("kwargs", [
    {"policy_id": ""}, {"generation": 0}, {"allowed_endpoints": ("submit",)},
    {"claim_ttl": timedelta(0)}, {"heartbeat_ttl": timedelta(0)},
    {"maximum_runtime": timedelta(minutes=11)}, {"maximum_attempts": 0},
    {"spool_max_files": 0}, {"spool_max_bytes": 0}, {"multipart_part_bytes": 1},
    {"paper_only": False}, {"mutations_allowed": True},
    {"external_order_routing_allowed": True}, {"live_trading_allowed": True},
])
def test_policy_rejects_unsafe_values(kwargs):
    with pytest.raises(ValueError):
        policy(**kwargs)


def test_policy_digest_is_deterministic():
    assert policy().digest == policy().digest
    assert policy(generation=8).digest != policy().digest


def test_keyring_requires_strong_keys():
    with pytest.raises(ValueError): HmacKeyRingV104({})
    with pytest.raises(ValueError): HmacKeyRingV104({"k": b"short"})


def test_sign_and_verify_attestation_and_claim():
    p = policy(); ring, claim, att = signed_inputs(p)
    ring.verify_claim(claim); ring.verify_attestation(att)


def test_signature_tampering_rejected():
    p = policy(); ring, claim, att = signed_inputs(p)
    with pytest.raises(AuthorizationError): ring.verify_claim(claim.__class__(**{**{f:getattr(claim,f) for f in claim.__dataclass_fields__}, "campaign_id":"x"}))
    with pytest.raises(AuthorizationError): ring.verify_attestation(att.__class__(**{**{f:getattr(att,f) for f in att.__dataclass_fields__}, "worker_id":"x"}))


def test_unknown_key_rejected():
    p = policy(); _, claim, _ = signed_inputs(p)
    with pytest.raises(AuthorizationError): HmacKeyRingV104({"other": SECRET}).verify_claim(claim)


def test_replay_ledger_rejects_claim_and_nonce_reuse():
    ledger = ReplayLedgerV104(); ledger.consume("c1", "n1")
    with pytest.raises(ReplayError): ledger.consume("c1", "n2")
    with pytest.raises(ReplayError): ledger.consume("c2", "n1")


def test_heartbeat_guard_accepts_monotonic_sequence():
    guard = HeartbeatGuardV104()
    guard.accept(WorkerHeartbeatV104("w", "c", 1, 2, 1, NOW), NOW, timedelta(seconds=30))
    guard.accept(WorkerHeartbeatV104("w", "c", 1, 2, 2, NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1), timedelta(seconds=30))


@pytest.mark.parametrize("heartbeat,now", [
    (WorkerHeartbeatV104("w", "c", 1, 2, 1, NOW + timedelta(seconds=2)), NOW),
    (WorkerHeartbeatV104("w", "c", 1, 2, 1, NOW - timedelta(seconds=31)), NOW),
])
def test_heartbeat_guard_rejects_future_or_stale(heartbeat, now):
    with pytest.raises(StaleClaimError): HeartbeatGuardV104().accept(heartbeat, now, timedelta(seconds=30))


def test_heartbeat_guard_rejects_regressions_and_identity_change():
    guard = HeartbeatGuardV104(); guard.accept(WorkerHeartbeatV104("w", "c", 1, 2, 2, NOW), NOW, timedelta(seconds=30))
    for heartbeat in [
        WorkerHeartbeatV104("w2", "c", 1, 2, 3, NOW), WorkerHeartbeatV104("w", "c2", 1, 2, 3, NOW),
        WorkerHeartbeatV104("w", "c", 2, 2, 3, NOW), WorkerHeartbeatV104("w", "c", 1, 3, 3, NOW),
        WorkerHeartbeatV104("w", "c", 1, 2, 2, NOW), WorkerHeartbeatV104("w", "c", 1, 2, 3, NOW - timedelta(seconds=1)),
    ]:
        with pytest.raises(StaleClaimError): guard.accept(heartbeat, NOW, timedelta(seconds=30))


def test_runner_only_calls_paper_read_endpoints_and_redacts():
    transport = FakeTransport(); runner = ReadOnlyAlpacaRunnerV104(transport, "id", "secret")
    result = runner.probe(("account", "orders", "positions", "clock"))
    assert all(call[0].startswith(PAPER_REST_BASE) for call in transport.calls)
    assert all(call[3] is True and call[4] is False for call in transport.calls)
    assert result["account"]["secret"] == "[REDACTED]"
    assert result["account"]["nested"][0]["token"] == "[REDACTED]"


def test_runner_rejects_invalid_endpoints_and_mutations():
    runner = ReadOnlyAlpacaRunnerV104(FakeTransport(), "id", "secret")
    for endpoints in [(), ("submit",)]:
        with pytest.raises(AuthorizationError): runner.probe(endpoints)
    for method in (runner.submit, runner.replace, runner.cancel):
        with pytest.raises(AuthorizationError): method()


def test_runner_constructor_validates_credentials_and_timeout():
    with pytest.raises(ValueError): ReadOnlyAlpacaRunnerV104(FakeTransport(), "", "x")
    with pytest.raises(ValueError): ReadOnlyAlpacaRunnerV104(FakeTransport(), "x", "", 5)
    with pytest.raises(ValueError): ReadOnlyAlpacaRunnerV104(FakeTransport(), "x", "y", 100)
    with pytest.raises(ValueError): ReadOnlyAlpacaRunnerV104(FakeTransport(), "x", "y", maximum_response_bytes=0)



def test_runner_rejects_non_mapping_and_oversized_response():
    class NonMapping:
        def get(self, url, *, headers, timeout_seconds, tls_verify, allow_redirects): return []
    with pytest.raises(PermanentTransportError): ReadOnlyAlpacaRunnerV104(NonMapping(), "id", "secret").probe(("account",))
    class Huge:
        def get(self, url, *, headers, timeout_seconds, tls_verify, allow_redirects): return {"x": "y" * 100}
    with pytest.raises(PermanentTransportError): ReadOnlyAlpacaRunnerV104(Huge(), "id", "secret", maximum_response_bytes=20).probe(("account",))


def test_uploader_constructor_validates_key_and_part_size(tmp_path):
    with pytest.raises(ValueError): ResumableUploaderV104(tmp_path, FakeObjectStore(), 0, SECRET)
    with pytest.raises(ValueError): ResumableUploaderV104(tmp_path, FakeObjectStore(), 1, b"short")

def test_journal_append_verify_and_tamper(tmp_path):
    journal = WorkerEventJournalV104(tmp_path / "events.jsonl")
    first = journal.append("A", WorkerState.IDLE, NOW, {"secret": "x"})
    second = journal.append("B", WorkerState.CLAIMED, NOW, {"claim_id": "c"})
    assert first.sequence == 1 and second.previous_digest == first.event_digest
    assert journal.verify()[0].attributes["secret"] == "[REDACTED]"
    data = journal.path.read_bytes().replace(b'"event_type":"A"', b'"event_type":"X"')
    journal.path.write_bytes(data)
    with pytest.raises(IntegrityError): journal.verify()


def test_journal_rejects_invalid_json(tmp_path):
    path = tmp_path / "e"; path.write_text("{\n")
    with pytest.raises(IntegrityError): WorkerEventJournalV104(path).verify()


def test_spool_enqueue_replay_ack_and_pending(tmp_path):
    spool = EvidenceSpoolV104(tmp_path / "s", 10, 1000)
    item = spool.enqueue("r1", "c1", b"abc", NOW, timedelta(days=1))
    assert spool.enqueue("r1", "c1", b"abc", NOW, timedelta(days=1)) == item
    assert spool.pending()[0].record_id == "r1"
    ack = spool.acknowledge("r1", NOW)
    assert ack.acknowledged and not spool.pending()
    assert spool.acknowledge("r1", NOW).acknowledged


def test_spool_replay_conflict_and_capacity(tmp_path):
    spool = EvidenceSpoolV104(tmp_path / "s", 1, 3)
    spool.enqueue("r1", "c1", b"abc", NOW, timedelta(days=1))
    with pytest.raises(IntegrityError): spool.enqueue("r1", "c1", b"abd", NOW, timedelta(days=1))
    with pytest.raises(CapacityError): spool.enqueue("r2", "c2", b"x", NOW, timedelta(days=1))
    with pytest.raises(KeyError): spool.acknowledge("missing", NOW)


def test_spool_detects_missing_and_modified_payload(tmp_path):
    spool = EvidenceSpoolV104(tmp_path / "s", 10, 1000)
    item = spool.enqueue("r1", "c1", b"abc", NOW, timedelta(days=1))
    path = spool.root / item.payload_path
    path.write_bytes(b"xxx")
    with pytest.raises(IntegrityError): spool.verify()


def test_spool_detects_manifest_corruption(tmp_path):
    spool = EvidenceSpoolV104(tmp_path / "s", 10, 1000)
    spool.enqueue("r1", "c1", b"abc", NOW, timedelta(days=1))
    spool.manifest_path.write_text("{\n")
    with pytest.raises(IntegrityError): spool.verify()


def test_resumable_uploader_completes_and_reuses_checkpoint(tmp_path):
    spool = EvidenceSpoolV104(tmp_path / "s", 10, 1000)
    record = spool.enqueue("r1", "c1", b"abcdefgh", NOW, timedelta(days=1))
    store = FakeObjectStore(); uploader = ResumableUploaderV104(tmp_path / "u", store, 3, SECRET)
    checkpoint = uploader.upload(record, spool.root)
    assert checkpoint.completed_object_digest == record.payload_digest
    assert len(checkpoint.completed_parts) == 3
    assert uploader.upload(record, spool.root) == checkpoint
    assert store.create_calls == 1 and store.complete_calls == 1


def test_resumable_uploader_rejects_empty_etag_bad_digest_and_checkpoint_conflict(tmp_path):
    spool = EvidenceSpoolV104(tmp_path / "s", 10, 1000)
    record = spool.enqueue("r1", "c1", b"abc", NOW, timedelta(days=1))
    store = FakeObjectStore(); store.empty_etag = True
    with pytest.raises(TransientTransportError): ResumableUploaderV104(tmp_path / "u1", store, 2, SECRET).upload(record, spool.root)
    store2 = FakeObjectStore(); store2.bad_digest = True
    with pytest.raises(IntegrityError): ResumableUploaderV104(tmp_path / "u2", store2, 2, SECRET).upload(record, spool.root)
    root = tmp_path / "u3"; uploader = ResumableUploaderV104(root, FakeObjectStore(), 2, SECRET)
    uploader.upload(record, spool.root)
    checkpoint = root / "r1.json"; envelope = json.loads(checkpoint.read_text()); envelope["payload"]["payload_digest"] = "0" * 64; checkpoint.write_text(json.dumps(envelope))
    with pytest.raises(IntegrityError): uploader.upload(record, spool.root)


def test_dlq_enqueue_deduplicate_release_and_verify(tmp_path):
    queue = DeadLetterQueueV104(tmp_path / "dlq")
    first = queue.enqueue("r1", "c1", DlqReason.TRANSIENT_EXHAUSTED, 3, NOW, "x")
    assert queue.enqueue("r1", "c1", DlqReason.TRANSIENT_EXHAUSTED, 3, NOW, "x") == first
    assert queue.pending()[0].record_id == "r1"
    released = queue.release("r1", "operator", NOW)
    assert released.released and not queue.pending()
    with pytest.raises(KeyError): queue.release("r1", "operator", NOW)
    with pytest.raises(AuthorizationError): queue.release("missing", "", NOW)


def test_dlq_detects_tampering_and_json_error(tmp_path):
    queue = DeadLetterQueueV104(tmp_path / "dlq")
    queue.enqueue("r1", "c1", DlqReason.CRASH_RECOVERY, 0, NOW, "x")
    queue.path.write_bytes(queue.path.read_bytes().replace(b'"detail":"x"', b'"detail":"y"'))
    with pytest.raises(IntegrityError): queue.verify()
    queue.path.write_text("{\n")
    with pytest.raises(IntegrityError): queue.verify()


def test_plane_success_end_to_end(tmp_path):
    plane, claim, att, transport, store = build_plane(tmp_path)
    result = plane.execute(claim, att, NOW)
    assert result.outcome is ClaimOutcome.VERIFIED
    assert result.state is WorkerState.COMPLETED
    assert result.evidence_digest
    assert len(transport.calls) == 4
    assert store.complete_calls == 1
    assert not plane.spool.pending()
    assert plane.journal.verify()[-1].event_type == "CLAIM_COMPLETED"


def test_plane_transient_retry_then_success(tmp_path):
    plane, claim, att, _, _ = build_plane(tmp_path, transport=FakeTransport(failures=2))
    result = plane.execute(claim, att, NOW)
    assert result.attempt == 3 and result.outcome is ClaimOutcome.VERIFIED


def test_plane_transient_exhaustion_to_dlq(tmp_path):
    plane, claim, att, _, _ = build_plane(tmp_path, transport=FakeTransport(failures=99))
    result = plane.execute(claim, att, NOW)
    assert result.outcome is ClaimOutcome.RECOVERY_REQUIRED
    assert plane.dlq.pending()[0].reason is DlqReason.TRANSIENT_EXHAUSTED
    with pytest.raises(StaleClaimError): plane.heartbeat(WorkerHeartbeatV104(claim.worker_id, claim.claim_id, claim.generation, claim.fencing_token, 1, NOW), NOW)


def test_plane_permanent_failure(tmp_path):
    plane, claim, att, _, _ = build_plane(tmp_path, transport=FakeTransport(permanent=True))
    result = plane.execute(claim, att, NOW)
    assert result.state is WorkerState.RECOVERY_REQUIRED
    assert plane.dlq.pending()[0].reason is DlqReason.PERMANENT_TRANSPORT


def test_plane_rejects_replay_and_active_worker(tmp_path):
    plane, claim, att, _, _ = build_plane(tmp_path)
    plane.execute(claim, att, NOW)
    with pytest.raises(AuthorizationError): plane.execute(claim, att, NOW)
    plane2, claim2, att2, _, _ = build_plane(tmp_path / "other")
    plane2.state = WorkerState.RUNNING
    with pytest.raises(WorkerPlaneError): plane2.execute(claim2, att2, NOW)


@pytest.mark.parametrize("changes", [
    {"generation": 8}, {"fencing_token": 0}, {"policy_digest": "0" * 64},
    {"worker_id": "other"}, {"deployment_id": "other"}, {"endpoints": ("submit",)},
    {"issued_at": NOW + timedelta(seconds=1)}, {"expires_at": NOW + timedelta(minutes=10)},
    {"not_before": NOW + timedelta(seconds=1)}, {"expires_at": NOW},
])
def test_plane_rejects_invalid_claims(tmp_path, changes):
    p = policy(); ring, claim, att = signed_inputs(p, claim_changes=changes)
    plane, _, _, _, _ = build_plane(tmp_path, p=p)
    plane.keyring = ring
    with pytest.raises(AuthorizationError): plane.execute(claim, att, NOW)
    assert plane.state is WorkerState.QUARANTINED


@pytest.mark.parametrize("changes", [
    {"generation": 8}, {"policy_digest": "0" * 64}, {"worker_id": "other"}, {"deployment_id": "other"},
    {"created_at": NOW + timedelta(seconds=2)}, {"expires_at": NOW},
])
def test_plane_rejects_invalid_attestation(tmp_path, changes):
    p = policy(); ring, claim, att = signed_inputs(p, attestation_changes=changes)
    plane, _, _, _, _ = build_plane(tmp_path, p=p); plane.keyring = ring
    with pytest.raises(AuthorizationError): plane.execute(claim, att, NOW)


def test_plane_heartbeat_requires_active_matching_claim(tmp_path):
    plane, claim, att, _, _ = build_plane(tmp_path)
    with pytest.raises(StaleClaimError): plane.heartbeat(WorkerHeartbeatV104("w", "c", 1, 1, 1, NOW), NOW)
    plane._active_claim = claim; plane.state = WorkerState.RUNNING
    with pytest.raises(StaleClaimError): plane.heartbeat(WorkerHeartbeatV104("other", claim.claim_id, claim.generation, claim.fencing_token, 1, NOW), NOW)
    with pytest.raises(StaleClaimError): plane.heartbeat(WorkerHeartbeatV104(claim.worker_id, claim.claim_id, claim.generation + 1, claim.fencing_token, 1, NOW), NOW)
    plane.heartbeat(WorkerHeartbeatV104(claim.worker_id, claim.claim_id, claim.generation, claim.fencing_token, 1, NOW), NOW)


def test_plane_recover_after_crash_and_terminal_replay(tmp_path):
    plane, claim, _, _, _ = build_plane(tmp_path)
    assert plane.recover_after_crash(NOW) is WorkerState.IDLE
    plane.journal.append("CLAIM_ACCEPTED", WorkerState.CLAIMED, NOW, {"claim_id": claim.claim_id})
    assert plane.recover_after_crash(NOW) is WorkerState.RECOVERY_REQUIRED
    event_count = len(plane.journal.verify())
    assert plane.recover_after_crash(NOW) is WorkerState.RECOVERY_REQUIRED
    assert len(plane.journal.verify()) == event_count
    assert plane.dlq.pending()[0].reason is DlqReason.CRASH_RECOVERY
    plane2, claim2, att2, _, _ = build_plane(tmp_path / "complete")
    plane2.execute(claim2, att2, NOW)
    assert plane2.recover_after_crash(NOW) is WorkerState.COMPLETED


def test_concurrent_replay_only_one_claim_succeeds(tmp_path):
    p = policy(); ring, claim, att = signed_inputs(p); ledger = ReplayLedgerV104()
    outcomes=[]
    def consume():
        try: ledger.consume(claim.claim_id, claim.nonce); outcomes.append("ok")
        except ReplayError: outcomes.append("replay")
    threads=[threading.Thread(target=consume) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert outcomes.count("ok") == 1 and outcomes.count("replay") == 7
