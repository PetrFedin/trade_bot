from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from app.runtime.postgres_deployment_qualification_v106 import (
    ClaimedRolloutActionV106,
    PostgresDeploymentQualificationRepositoryV106,
    PostgresRepositoryErrorV106,
    StaleFenceErrorV106,
)

NOW = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64


class FakeCursor:
    def __init__(self, *, rowcounts=None, rows=None, fail_on_execute=None):
        self.rowcounts = list(rowcounts or [1])
        self.rows = list(rows or [])
        self.fail_on_execute = fail_on_execute
        self.executions = []
        self.rowcount = 0
        self.closed = False

    def execute(self, query, params=None):
        self.executions.append((query, params))
        if self.fail_on_execute is not None and len(self.executions) == self.fail_on_execute:
            raise RuntimeError("database failure")
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 1

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def repo(*, rowcounts=None, rows=None, fail_on_execute=None):
    cursor = FakeCursor(rowcounts=rowcounts, rows=rows, fail_on_execute=fail_on_execute)
    connection = FakeConnection(cursor)
    return PostgresDeploymentQualificationRepositoryV106(connection), connection, cursor


def assert_committed(connection, cursor):
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert cursor.closed


def test_consume_manifest_replay_commits_and_uses_on_conflict():
    repository, connection, cursor = repo()
    repository.consume_manifest_replay(manifest_id="m1", nonce="n1", consumed_at=NOW)
    assert "ON CONFLICT DO NOTHING" in cursor.executions[0][0]
    assert cursor.executions[0][1] == ("m1", "n1", NOW)
    assert_committed(connection, cursor)


def test_consume_manifest_replay_rejects_duplicate_and_rolls_back():
    repository, connection, cursor = repo(rowcounts=[0])
    with pytest.raises(PostgresRepositoryErrorV106):
        repository.consume_manifest_replay(manifest_id="m1", nonce="n1", consumed_at=NOW)
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert cursor.closed


def test_create_qualification_and_failure_path():
    repository, connection, cursor = repo()
    repository.create_qualification(qualification_id="q1", manifest_id="m1", policy_digest=HEX_A, manifest_digest=HEX_B, generation=7, state="PLANNED", created_at=NOW)
    assert "deployment_qualification" in cursor.executions[0][0]
    assert cursor.executions[0][1][4] == 7
    assert_committed(connection, cursor)

    repository, connection, _ = repo(rowcounts=[0])
    with pytest.raises(PostgresRepositoryErrorV106):
        repository.create_qualification(qualification_id="q1", manifest_id="m1", policy_digest=HEX_A, manifest_digest=HEX_B, generation=7, state="PLANNED", created_at=NOW)
    assert connection.rollbacks == 1


def test_append_event_and_observation():
    repository, connection, cursor = repo()
    repository.append_event(qualification_id="q1", sequence=1, event_type="START", observed_at=NOW, payload_digest=HEX_A, previous_digest=HEX_B, event_digest=HEX_C)
    assert "qualification_event" in cursor.executions[0][0]
    assert_committed(connection, cursor)

    repository, connection, cursor = repo()
    repository.append_observation(qualification_id="q1", sample_id="s1", observed_at=NOW, sample_digest=HEX_A, gate_digest=HEX_B, passed=True)
    assert "observation_sample" in cursor.executions[0][0]
    assert cursor.executions[0][1][-1] is True
    assert_committed(connection, cursor)


@pytest.mark.parametrize("method", ["event", "observation"])
def test_append_methods_reject_zero_rowcount(method):
    repository, connection, _ = repo(rowcounts=[0])
    with pytest.raises(PostgresRepositoryErrorV106):
        if method == "event":
            repository.append_event(qualification_id="q1", sequence=1, event_type="START", observed_at=NOW, payload_digest=HEX_A, previous_digest=HEX_B, event_digest=HEX_C)
        else:
            repository.append_observation(qualification_id="q1", sample_id="s1", observed_at=NOW, sample_digest=HEX_A, gate_digest=HEX_B, passed=False)
    assert connection.rollbacks == 1


def test_enqueue_rollout_action():
    repository, connection, cursor = repo()
    repository.enqueue_rollout_action(action_id="a1", qualification_id="q1", action_type="PROMOTE", generation=7, fencing_token=9, idempotency_key="q1:PROMOTE:7", payload_digest=HEX_A, signature=HEX_B, created_at=NOW)
    query, params = cursor.executions[0]
    assert "rollout_action_outbox" in query
    assert "'PENDING', 0" in query
    assert params[3:5] == (7, 9)
    assert_committed(connection, cursor)


def test_claim_rollout_action_returns_none_without_due_work():
    repository, connection, cursor = repo(rows=[])
    assert repository.claim_rollout_action(worker_id="worker-1", generation=7, fencing_token=9, claimed_at=NOW) is None
    assert "FOR UPDATE SKIP LOCKED" in cursor.executions[0][0]
    assert_committed(connection, cursor)


def test_claim_rollout_action_is_fenced_and_single_attempt():
    row = ("a1", "q1", "PROMOTE", 7, 9, HEX_A)
    repository, connection, cursor = repo(rowcounts=[1, 1], rows=[row])
    action = repository.claim_rollout_action(worker_id="worker-1", generation=7, fencing_token=9, claimed_at=NOW)
    assert action == ClaimedRolloutActionV106("a1", "q1", "PROMOTE", 7, 9, HEX_A)
    assert "attempt_count = attempt_count + 1" in cursor.executions[1][0]
    assert "attempt_count = 0" in cursor.executions[1][0]
    assert_committed(connection, cursor)


def test_claim_rollout_action_rejects_lost_fence():
    row = ("a1", "q1", "PROMOTE", 7, 9, HEX_A)
    repository, connection, cursor = repo(rowcounts=[1, 0], rows=[row])
    with pytest.raises(StaleFenceErrorV106):
        repository.claim_rollout_action(worker_id="worker-1", generation=7, fencing_token=9, claimed_at=NOW)
    assert connection.rollbacks == 1
    assert cursor.closed


@pytest.mark.parametrize("success,expected", [(True, "ACKED"), (False, "FAILED")])
def test_acknowledge_rollout_action(success, expected):
    repository, connection, cursor = repo()
    repository.acknowledge_rollout_action(action_id="a1", generation=7, fencing_token=9, success=success, receipt_digest=HEX_C, acknowledged_at=NOW)
    query, params = cursor.executions[0]
    assert "attempt_count = 1" in query
    assert params[0] == expected
    assert_committed(connection, cursor)


def test_acknowledge_rejects_stale_fence():
    repository, connection, _ = repo(rowcounts=[0])
    with pytest.raises(StaleFenceErrorV106):
        repository.acknowledge_rollout_action(action_id="a1", generation=7, fencing_token=9, success=True, receipt_digest=HEX_C, acknowledged_at=NOW)
    assert connection.rollbacks == 1


def test_certificate_and_disaster_recovery_events():
    repository, connection, cursor = repo()
    repository.record_certificate_drill_event(drill_id="d1", sequence=1, state="ISSUED", worker_id="w1", identity_generation=2, evidence_digest=HEX_A, observed_at=NOW)
    assert "certificate_drill_event" in cursor.executions[0][0]
    assert_committed(connection, cursor)

    repository, connection, cursor = repo()
    repository.record_disaster_recovery_event(drill_id="dr1", sequence=1, state="RESTORING", backup_id="b1", evidence={"digest": HEX_A, "ok": True}, observed_at=NOW)
    query, params = cursor.executions[0]
    assert "evidence_json" in query
    assert json.loads(params[4]) == {"digest": HEX_A, "ok": True}
    assert_committed(connection, cursor)


@pytest.mark.parametrize("method", ["enqueue", "certificate", "dr"])
def test_remaining_insert_methods_reject_zero_rowcount(method):
    repository, connection, _ = repo(rowcounts=[0])
    with pytest.raises(PostgresRepositoryErrorV106):
        if method == "enqueue":
            repository.enqueue_rollout_action(action_id="a1", qualification_id="q1", action_type="PROMOTE", generation=7, fencing_token=9, idempotency_key="i1", payload_digest=HEX_A, signature=HEX_B, created_at=NOW)
        elif method == "certificate":
            repository.record_certificate_drill_event(drill_id="d1", sequence=1, state="ISSUED", worker_id="w1", identity_generation=2, evidence_digest=HEX_A, observed_at=NOW)
        else:
            repository.record_disaster_recovery_event(drill_id="d1", sequence=1, state="RESTORING", backup_id="b1", evidence={}, observed_at=NOW)
    assert connection.rollbacks == 1


def test_database_exception_rolls_back_and_closes_cursor():
    repository, connection, cursor = repo(fail_on_execute=1)
    with pytest.raises(RuntimeError, match="database failure"):
        repository.consume_manifest_replay(manifest_id="m1", nonce="n1", consumed_at=NOW)
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert cursor.closed
