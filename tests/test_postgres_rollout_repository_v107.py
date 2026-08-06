from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.runtime.postgres_rollout_repository_v107 import (
    PostgreSQLConflictV107,
    PostgreSQLNotFoundV107,
    PostgreSQLRepositoryErrorV107,
    PostgreSQLRolloutRepositoryV107,
    command_from_json_v107,
    command_to_json_v107,
)
from app.runtime.rollout_execution_v107 import ExecutionReceiptV107, ReceiptStatusV107
from tests.conftest import EXECUTOR_SECRET, NOW


class FakeCursor:
    def __init__(self, script=None, default_rowcount=1):
        self.script = list(script or [])
        self.default_rowcount = default_rowcount
        self.rowcount = default_rowcount
        self.queries = []
        self._fetchone = None
        self._fetchall = []
        self.closed = False

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.queries.append((normalized, params))
        self.rowcount = self.default_rowcount
        self._fetchone = None
        self._fetchall = []
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            self.rowcount = item.get("rowcount", self.default_rowcount)
            self._fetchone = item.get("one")
            self._fetchall = item.get("all", [])
        return self

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, connections):
        self.connections = list(connections)

    def __call__(self):
        return self.connections.pop(0)


def test_command_json_roundtrip(command):
    decoded = command_from_json_v107(command_to_json_v107(command))
    assert decoded == command


@pytest.mark.parametrize("value", [
    "not-json",
    "[]",
    '{"intent": {}}',
    '{"intent":{"action":"INVALID"},"approvals":[]}',
])
def test_command_json_rejects_corruption(value):
    with pytest.raises(PostgreSQLRepositoryErrorV107):
        command_from_json_v107(value)


def test_enqueue_is_single_transaction_with_replay_and_outbox(command):
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    repo.enqueue(command, NOW)
    assert connection.commits == 1 and connection.rollbacks == 0
    sql = "\n".join(query for query, _ in cursor.queries)
    assert "INSERT INTO astra_rollout_replay_v107" in sql
    assert "INSERT INTO astra_rollout_execution_v107" in sql
    assert "INSERT INTO astra_rollout_outbox_v107" in sql
    execution_query, execution_params = cursor.queries[2]
    assert "deployment_uid" in execution_query
    assert command.intent.deployment_uid in execution_params
    assert cursor.queries[0][0] == "BEGIN"


def test_enqueue_rolls_back_on_any_failure(command):
    cursor = FakeCursor(script=[{}, RuntimeError("duplicate")])
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    with pytest.raises(RuntimeError):
        repo.enqueue(command, NOW)
    assert connection.rollbacks == 1 and connection.commits == 0


def test_claim_next_uses_skip_locked_and_returns_command(command):
    command_json = command_to_json_v107(command)
    cursor = FakeCursor(script=[{}, {"one": ("cmd-001", command_json, "PENDING", 0, None, None)}, {"rowcount": 1}])
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    claimed = repo.claim_next(worker_id="worker-1", observed_at=NOW)
    assert claimed is not None and claimed.command == command
    assert claimed.claimed_by == "worker-1"
    assert any("FOR UPDATE SKIP LOCKED" in query for query, _ in cursor.queries)
    assert connection.commits == 1


def test_claim_next_none_commits_cleanly():
    cursor = FakeCursor(script=[{}, {"one": None}])
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    assert repo.claim_next(worker_id="worker-1", observed_at=NOW) is None
    assert connection.commits == 1


def test_claim_conflict_rolls_back(command):
    cursor = FakeCursor(script=[{}, {"one": ("cmd-001", command_to_json_v107(command), "PENDING", 0, None, None)}, {"rowcount": 0}])
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    with pytest.raises(PostgreSQLConflictV107):
        repo.claim_next(worker_id="worker-1", observed_at=NOW)
    assert connection.rollbacks == 1


def test_load_requires_existing_claimed_row(command):
    cursor = FakeCursor(script=[{"one": None}])
    repo = PostgreSQLRolloutRepositoryV107(Factory([FakeConnection(cursor)]))
    with pytest.raises(PostgreSQLNotFoundV107):
        repo.load("cmd-001")

    cursor = FakeCursor(script=[{"one": (command_to_json_v107(command), "PENDING", None, 0, None, None)}])
    repo = PostgreSQLRolloutRepositoryV107(Factory([FakeConnection(cursor)]))
    with pytest.raises(PostgreSQLConflictV107, match="not claimed"):
        repo.load("cmd-001")


def test_record_preflight_is_fenced_by_worker():
    cursor = FakeCursor(script=[{}, {"rowcount": 0}])
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    with pytest.raises(PostgreSQLConflictV107):
        repo.record_preflight(
            command_id="cmd-001", worker_id="wrong", passed=True,
            gates_digest="a" * 64, pre_snapshot_digest="b" * 64, observed_at=NOW,
        )
    assert connection.rollbacks == 1


def test_mutation_marker_is_single_attempt_and_durable():
    cursor = FakeCursor(script=[{}, {"one": (11,)}, {"rowcount": 1}])
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    repo.mark_mutation_started(
        command_id="cmd-001", worker_id="worker-1", deployment_uid="uid-123",
        fencing_token=11, patch_digest="a" * 64, observed_at=NOW,
    )
    fence_query = cursor.queries[1][0]
    update_query = cursor.queries[2][0]
    assert "ON CONFLICT (deployment_uid) DO UPDATE" in fence_query
    assert "fencing_token < EXCLUDED.fencing_token" in fence_query
    assert "mutation_attempts = 1" in update_query
    assert "mutation_attempts = 0" in update_query
    assert "state = 'PREFLIGHT'" in update_query
    assert connection.commits == 1


def test_mutation_marker_rejects_stale_durable_fence():
    cursor = FakeCursor(script=[{}, {"one": None}])
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    with pytest.raises(PostgreSQLConflictV107, match="not newer"):
        repo.mark_mutation_started(
            command_id="cmd-001", worker_id="worker-1", deployment_uid="uid-123",
            fencing_token=10, patch_digest="a" * 64, observed_at=NOW,
        )
    assert connection.rollbacks == 1


def test_mark_verifying_accepts_claim_or_recovery_worker():
    cursor = FakeCursor()
    repo = PostgreSQLRolloutRepositoryV107(Factory([FakeConnection(cursor)]))
    repo.mark_verifying(command_id="cmd-001", worker_id="worker-1", observed_at=NOW)
    query = cursor.queries[1][0]
    assert "claimed_by = %s OR recovery_by = %s" in query
    assert "MUTATION_STARTED" in query and "UNCERTAIN" in query


def test_recovery_claim_covers_crash_and_uncertain_states(command):
    cursor = FakeCursor(script=[{}, {"one": (command_to_json_v107(command), "MUTATION_STARTED", 1, "a" * 64, "b" * 64)}])
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    claimed = repo.claim_recovery(
        command_id="cmd-001", worker_id="recovery-1", observed_at=NOW, claim_ttl_seconds=30,
    )
    assert claimed.command == command and claimed.mutation_attempts == 1
    query, params = cursor.queries[1]
    assert "MUTATION_STARTED" in query and "VERIFYING" in query and "UNCERTAIN" in query
    assert params[-1] == NOW - timedelta(seconds=30)


def test_recovery_claim_rejects_busy_or_terminal_command():
    cursor = FakeCursor(script=[{}, {"one": None}])
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    with pytest.raises(PostgreSQLConflictV107, match="recovery"):
        repo.claim_recovery(
            command_id="cmd-001", worker_id="recovery-1", observed_at=NOW, claim_ttl_seconds=30,
        )


def test_list_recoverable_is_read_only_and_bounded():
    cursor = FakeCursor(script=[{"all": [("cmd-1",), ("cmd-2",)]}])
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    assert repo.list_recoverable(limit=2) == ("cmd-1", "cmd-2")
    assert connection.commits == 0 and connection.rollbacks == 0
    with pytest.raises(Exception):
        repo.list_recoverable(limit=0)


def test_complete_binds_receipt_and_appends_event(command, snapshot):
    receipt = ExecutionReceiptV107.sign(
        receipt_id="r1", command=command, worker_id="worker-1", status=ReceiptStatusV107.APPLIED,
        observed_at=NOW, pre_snapshot_digest=snapshot.snapshot_digest,
        post_snapshot_digest="f" * 64, patch_digest="e" * 64, mutation_attempted=True,
        reason="ok", executor_key_id="executor-key", executor_secret=EXECUTOR_SECRET,
    )
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    repo = PostgreSQLRolloutRepositoryV107(Factory([connection]))
    repo.complete(command_id="cmd-001", worker_id="worker-1", receipt=receipt, observed_at=NOW)
    assert "state = %s" in cursor.queries[1][0]
    assert cursor.queries[1][1][0] == "SUCCEEDED"
    assert "INSERT INTO astra_rollout_event_v107" in cursor.queries[2][0]
    assert connection.commits == 1
