from datetime import datetime, timezone
import pytest
from app.runtime.postgres_worker_plane_v104 import PostgresWorkerRepositoryV104

NOW = datetime(2026, 8, 3, 18, tzinfo=timezone.utc)

class Result:
    rowcount=1

class Cursor:
    def __init__(self, row=None, error=None, rowcount=1): self.row=row; self.error=error; self.queries=[]; self.closed=False; self.rowcount=rowcount
    def execute(self, query, params=None):
        self.queries.append((query, params))
        if self.error: raise self.error
        return self
    def fetchone(self): return self.row
    def fetchall(self): return []
    def close(self): self.closed=True

class Connection:
    def __init__(self, cursor): self._cursor=cursor; self.commits=0; self.rollbacks=0
    def cursor(self): return self._cursor
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


def test_transaction_commit_and_close():
    cursor=Cursor(); conn=Connection(cursor); repo=PostgresWorkerRepositoryV104(conn)
    with repo.transaction() as item: item.execute("SELECT 1")
    assert conn.commits == 1 and conn.rollbacks == 0 and cursor.closed


def test_transaction_rollback_and_close():
    cursor=Cursor(error=RuntimeError("boom")); conn=Connection(cursor); repo=PostgresWorkerRepositoryV104(conn)
    with pytest.raises(RuntimeError):
        with repo.transaction() as item: item.execute("SELECT 1")
    assert conn.commits == 0 and conn.rollbacks == 1 and cursor.closed


def test_claim_next_returns_row_and_uses_skip_locked():
    row=("c","camp","run",7,9,'{"x":1}'); cursor=Cursor(row=row); conn=Connection(cursor)
    claimed=PostgresWorkerRepositoryV104(conn).claim_next("w","d",NOW)
    assert claimed.claim_id == "c" and "FOR UPDATE SKIP LOCKED" in cursor.queries[0][0]


def test_claim_next_none():
    assert PostgresWorkerRepositoryV104(Connection(Cursor())).claim_next("w","d",NOW) is None


def test_heartbeat_returns_false_when_fenced():
    assert not PostgresWorkerRepositoryV104(Connection(Cursor(rowcount=0))).heartbeat("c","w",1,2,3,NOW)


def test_heartbeat_and_persistence_queries():
    cursor=Cursor(); repo=PostgresWorkerRepositoryV104(Connection(cursor))
    assert repo.heartbeat("c","w",1,2,3,NOW)
    repo.record_spool("r","c","a"*64,3,NOW)
    repo.enqueue_dlq("d","c","reason","detail",NOW)
    repo.release_dlq("d",1,"operator","resolved",NOW)
    joined="\n".join(query for query,_ in cursor.queries)
    assert "heartbeat_sequence" in joined and "evidence_spool" in joined and "worker_dead_letter" in joined and "worker_dead_letter_release" in joined


def test_release_dlq_validates_operator_and_sequence():
    repo=PostgresWorkerRepositoryV104(Connection(Cursor()))
    with pytest.raises(ValueError): repo.release_dlq("d",0,"operator","x",NOW)
    with pytest.raises(ValueError): repo.release_dlq("d",1,"","x",NOW)
