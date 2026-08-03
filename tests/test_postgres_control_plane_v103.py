from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.runtime.campaign_control_plane_v103 import LeaseUnavailable, StaleFencingToken, StaleGeneration
from app.runtime.postgres_control_plane_v103 import PostgresControlPlaneRepositoryV103

UTC = timezone.utc
BASE = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executions = []

    def execute(self, query, params=None):
        self.executions.append((query, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows = tuple(self.rows)
        self.rows.clear()
        return rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class FakeConnection:
    def __init__(self, rows):
        self.cursor_value = FakeCursor(rows)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def repo_with_rows(rows):
    connection = FakeConnection(rows)
    return PostgresControlPlaneRepositoryV103(lambda: connection), connection


def test_claim_due_campaign_returns_receipt_and_commits():
    repo, connection = repo_with_rows([
        ("campaign", "worker", 1, 4, BASE, BASE + timedelta(minutes=5))
    ])
    receipt = repo.claim_due_campaign("campaign", "worker", 1, BASE, timedelta(minutes=5))
    assert receipt.fencing_token == 4
    assert connection.committed and connection.closed
    assert "claim_campaign_lease" in connection.cursor_value.executions[0][0]


def test_claim_due_campaign_rejects_empty_result():
    repo, connection = repo_with_rows([])
    with pytest.raises(LeaseUnavailable):
        repo.claim_due_campaign("campaign", "worker", 1, BASE, timedelta(minutes=5))
    assert connection.rolled_back and connection.closed


@pytest.mark.parametrize(
    "code,error",
    [
        ("STALE_GENERATION", StaleGeneration),
        ("STALE_FENCING_TOKEN", StaleFencingToken),
        ("LEASE_EXPIRED", LeaseUnavailable),
    ],
)
def test_heartbeat_maps_fail_closed_codes(code, error):
    repo, connection = repo_with_rows([(code, None)])
    with pytest.raises(error):
        repo.heartbeat("c", "w", 1, 1, "d", "b", BASE, timedelta(minutes=5))
    assert connection.rolled_back


def test_heartbeat_returns_extended_expiry():
    expiry = BASE + timedelta(minutes=5)
    repo, connection = repo_with_rows([("OK", expiry)])
    assert repo.heartbeat("c", "w", 1, 1, "d", "b", BASE, timedelta(minutes=5)) == expiry
    assert connection.committed


def test_append_event_returns_sequence():
    repo, connection = repo_with_rows([(12,)])
    sequence = repo.append_event("c", "EVENT", 1, 2, BASE, "{}", "0" * 64, "a" * 64)
    assert sequence == 12
    assert "append_control_plane_event" in connection.cursor_value.executions[0][0]


def test_append_event_rejects_empty_result():
    repo, _ = repo_with_rows([])
    with pytest.raises(RuntimeError):
        repo.append_event("c", "EVENT", 1, 2, BASE, "{}", "0" * 64, "a" * 64)


def test_due_campaign_ids_uses_skip_locked():
    repo, connection = repo_with_rows([("a",), ("b",)])
    assert repo.due_campaign_ids(BASE, 2) == ("a", "b")
    query = connection.cursor_value.executions[0][0]
    assert "FOR UPDATE SKIP LOCKED" in query


@pytest.mark.parametrize("limit", [0, 1001])
def test_due_campaign_ids_validates_limit(limit):
    repo, _ = repo_with_rows([])
    with pytest.raises(ValueError):
        repo.due_campaign_ids(BASE, limit)


def test_transaction_rolls_back_on_cursor_error():
    connection = FakeConnection([])

    class BadCursor(FakeCursor):
        def execute(self, query, params=None):
            raise RuntimeError("db failed")

    connection.cursor_value = BadCursor([])
    repo = PostgresControlPlaneRepositoryV103(lambda: connection)
    with pytest.raises(RuntimeError):
        repo.due_campaign_ids(BASE)
    assert connection.rolled_back and connection.closed
