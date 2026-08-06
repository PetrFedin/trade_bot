from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.runtime.postgres_signing_repository_v108 import PostgreSQLSigningRepositoryV108
from tests.helpers_v108 import NOW, authorization_bundle


@dataclass
class FakeCursor:
    executions: list[tuple[str, Any]] = field(default_factory=list)
    rowcount: int = 1
    _returning: bool = False

    def execute(self, query: str, params=None):
        self.executions.append((" ".join(query.split()), params))
        self._returning = "RETURNING generation" in query

    def fetchone(self):
        return (1,) if self._returning else None

    def close(self):
        return None


@dataclass
class FakeConnection:
    cursor_value: FakeCursor = field(default_factory=FakeCursor)
    commits: int = 0
    rollbacks: int = 0
    closed: int = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def test_snapshot_and_bundle_are_persisted_without_secrets() -> None:
    _, _, _, snapshot, _, bundle = authorization_bundle()
    connections: list[FakeConnection] = []

    def factory():
        connection = FakeConnection()
        connections.append(connection)
        return connection

    repository = PostgreSQLSigningRepositoryV108(factory)
    repository.persist_keyring_snapshot(snapshot, observed_at=NOW)
    repository.reserve_authorization_bundle(bundle, observed_at=NOW)
    assert [item.commits for item in connections] == [1, 1]
    bundle_sql = " ".join(query for query, _ in connections[1].cursor_value.executions)
    assert "astra_rollout_authorization_v108" in bundle_sql
    assert bundle_sql.count("astra_signature_replay_v108") == 3
    params_text = repr(connections[1].cursor_value.executions)
    assert "private" not in params_text.lower()


def test_repository_rejects_naive_time_and_rolls_back_conflicts() -> None:
    from datetime import datetime
    import pytest
    from app.runtime.postgres_signing_repository_v108 import (
        PostgreSQLSigningConflictV108,
        PostgreSQLSigningRepositoryErrorV108,
    )

    _, _, _, snapshot, _, bundle = authorization_bundle()
    connection = FakeConnection()
    repository = PostgreSQLSigningRepositoryV108(lambda: connection)
    with pytest.raises(PostgreSQLSigningRepositoryErrorV108):
        repository.reserve_authorization_bundle(bundle, observed_at=datetime(2026, 8, 6))

    class ConflictCursor(FakeCursor):
        def fetchone(self):
            return None

    conflict = FakeConnection(cursor_value=ConflictCursor())
    with pytest.raises(PostgreSQLSigningConflictV108):
        PostgreSQLSigningRepositoryV108(lambda: conflict).persist_keyring_snapshot(snapshot, observed_at=NOW)
    assert conflict.rollbacks == 1
    assert conflict.commits == 0
    assert conflict.closed == 1
