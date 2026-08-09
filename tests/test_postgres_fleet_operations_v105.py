from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.runtime.postgres_fleet_operations_v105 import FleetRepositoryErrorV105, PostgresFleetRepositoryV105

NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, *, rowcount=1, row=None, error=None):
        self.rowcount = rowcount
        self.row = row
        self.error = error
        self.executions = []
        self.closed = False

    def execute(self, query, params=None):
        self.executions.append((query, params))
        if self.error:
            raise self.error

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, cursor):
        self.value = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def repo(**cursor_kwargs):
    cursor = Cursor(**cursor_kwargs)
    connection = Connection(cursor)
    return PostgresFleetRepositoryV105(connection), connection, cursor


def test_transaction_commit_close_and_rollback_close():
    repository, connection, cursor = repo()
    with repository.transaction():
        pass
    assert connection.commits == 1 and connection.rollbacks == 0 and cursor.closed

    repository, connection, cursor = repo()
    with pytest.raises(RuntimeError):
        with repository.transaction():
            raise RuntimeError("boom")
    assert connection.commits == 0 and connection.rollbacks == 1 and cursor.closed


def test_consume_nonce_true_false_and_sql_boundary():
    repository, connection, cursor = repo(rowcount=1)
    assert repository.consume_enrollment_nonce("token", "nonce", NOW) is True
    assert "ON CONFLICT DO NOTHING" in cursor.executions[0][0]
    repository, connection, cursor = repo(rowcount=0)
    assert repository.consume_enrollment_nonce("token", "nonce", NOW) is False


def test_record_worker_success_and_stale_generation():
    repository, _connection, cursor = repo(rowcount=1)
    repository.record_worker("worker", "deploy", "zone", "a" * 64, 1, "ACTIVE", NOW)
    assert "identity_generation <=" in cursor.executions[0][0]
    repository, _connection, _cursor = repo(rowcount=0)
    with pytest.raises(FleetRepositoryErrorV105, match="stale"):
        repository.record_worker("worker", "deploy", "zone", "a" * 64, 1, "ACTIVE", NOW)


def test_record_heartbeat_fenced():
    repository, _connection, cursor = repo(rowcount=1)
    repository.record_heartbeat("worker", 1, 2, NOW)
    assert "heartbeat_sequence <" in cursor.executions[0][0]
    repository, _connection, _cursor = repo(rowcount=0)
    with pytest.raises(FleetRepositoryErrorV105, match="heartbeat"):
        repository.record_heartbeat("worker", 1, 2, NOW)


def test_claim_task_none_and_value_and_skip_locked():
    repository, _connection, cursor = repo(row=None)
    assert repository.claim_task("owner", NOW) is None
    assert "FOR UPDATE SKIP LOCKED" in cursor.executions[0][0]
    repository, _connection, _cursor = repo(row=("task", "DRAIN", 2, 3))
    result = repository.claim_task("owner", NOW)
    assert result.task_id == "task"
    assert result.generation == 2
    assert result.fencing_token == 3


def test_append_methods_commit_and_bind_values():
    calls = [
        ("append_containment", ("c", 1, "FLEET", "fleet", "reason", NOW)),
        ("append_containment_release", ("c", 1, "0" * 64, "a", "b", NOW)),
        ("append_scale_decision", ("d", "0" * 64, 1, 2, "reason", NOW)),
    ]
    for method, args in calls:
        repository, connection, cursor = repo()
        getattr(repository, method)(*args)
        assert connection.commits == 1
        assert cursor.executions


def test_evidence_object_success_conflict_and_execute_error_rollback():
    repository, connection, cursor = repo(rowcount=1)
    repository.record_evidence_object("key", "0" * 64, 1, "upload", NOW)
    assert connection.commits == 1
    repository, _connection, _cursor = repo(rowcount=0)
    with pytest.raises(FleetRepositoryErrorV105, match="replay"):
        repository.record_evidence_object("key", "0" * 64, 1, "upload", NOW)
    repository, connection, cursor = repo(error=RuntimeError("db"))
    with pytest.raises(RuntimeError):
        repository.consume_enrollment_nonce("t", "n", NOW)
    assert connection.rollbacks == 1 and cursor.closed
