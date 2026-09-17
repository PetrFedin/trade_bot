from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import app.oms.postgres as postgres_module
from app.oms.indexed import IndexedPostgresOmsStore
from app.oms.postgres import PostgresOmsStore

NOW = datetime(2026, 9, 16, 21, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _allow_offline_postgres_constructor(monkeypatch) -> None:
    """Keep these scripted unit tests independent of the optional psycopg extra."""

    monkeypatch.setattr(postgres_module, "psycopg", object())


def order_row(intent_id: str = "intent-1", broker_id: str = "broker-A") -> dict[str, object]:
    return {
        "intent_id": intent_id,
        "client_order_id": f"client-{intent_id}",
        "broker_order_id": broker_id,
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": Decimal("2"),
        "limit_price": Decimal("100"),
        "filled_quantity": Decimal("0"),
        "state": "ACKNOWLEDGED",
        "version": 3,
        "updated_at": NOW,
    }


@dataclass
class Step:
    one: object = None
    all_rows: list[object] | None = None
    rowcount: int = 1
    error: Exception | None = None


class ScriptedCursor:
    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.current = Step()
        self.rowcount = 0
        self.executed: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=None):
        self.executed.append((sql, params))
        if not self.steps:
            raise AssertionError(f"unexpected SQL: {sql}")
        self.current = self.steps.pop(0)
        self.rowcount = self.current.rowcount
        if self.current.error is not None:
            raise self.current.error
        return self

    def fetchone(self):
        return self.current.one

    def fetchall(self):
        return [] if self.current.all_rows is None else self.current.all_rows


class FakeConnection:
    def __init__(self, steps: list[Step]) -> None:
        self.cursor_value = ScriptedCursor(steps)
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_value

    def transaction(self):
        return self

    def commit(self) -> None:
        self.commits += 1


def store_with(monkeypatch, steps: list[Step]) -> tuple[IndexedPostgresOmsStore, FakeConnection]:
    store = IndexedPostgresOmsStore("postgresql://offline-unit")
    connection = FakeConnection(steps)
    monkeypatch.setattr(store, "_connect", lambda: connection)
    return store, connection


def proof(
    *,
    intent_id: str = "intent-1",
    predecessor: str = "broker-A",
    successor: str = "broker-B",
    kind: str = "REPLACE",
    state: str = "SUCCEEDED",
) -> dict[str, object]:
    return {
        "intent_id": intent_id,
        "kind": kind,
        "state": state,
        "broker_order_id": successor,
        "payload": {"broker_order_id": predecessor},
    }


def register(
    store: IndexedPostgresOmsStore,
    *,
    intent_id: str = "intent-1",
    mutation_id: str = "replace-1",
    predecessor: str = "broker-A",
    successor: str = "broker-B",
) -> None:
    store.register_replace_successor(
        intent_id=intent_id,
        mutation_id=mutation_id,
        predecessor_broker_order_id=predecessor,
        successor_broker_order_id=successor,
        occurred_at=NOW,
    )


def test_postgres_lookup_contract_uses_lineage_then_current_fallback(monkeypatch) -> None:
    row = order_row()
    store, _ = store_with(monkeypatch, [Step(one=row)])
    assert store.get_by_client_order_id("client-intent-1").intent_id == "intent-1"  # type: ignore[union-attr]

    store, _ = store_with(
        monkeypatch,
        [Step(one={"intent_id": "intent-1"}), Step(one=row)],
    )
    assert store.get_by_broker_order_id("broker-A").intent_id == "intent-1"  # type: ignore[union-attr]

    store, _ = store_with(monkeypatch, [Step(one=None), Step(all_rows=[])])
    assert store.get_by_broker_order_id("missing") is None

    store, _ = store_with(
        monkeypatch,
        [Step(one=None), Step(all_rows=[row, order_row("intent-2", "broker-A")])],
    )
    with pytest.raises(ValueError, match="OMS_BROKER_ORDER_ID_CONFLICT"):
        store.get_by_broker_order_id("broker-A")

    with pytest.raises(ValueError, match="client_order_id is required"):
        store.get_by_client_order_id(" ")
    with pytest.raises(ValueError, match="broker_order_id is required"):
        store.get_by_broker_order_id("")


def test_postgres_lineage_registration_validates_inputs_before_db(monkeypatch) -> None:
    store = IndexedPostgresOmsStore("postgresql://offline-unit")
    monkeypatch.setattr(
        store,
        "_connect",
        lambda: (_ for _ in ()).throw(AssertionError("db used")),
    )

    with pytest.raises(ValueError, match="intent_id and mutation_id are required"):
        register(store, intent_id="")
    with pytest.raises(ValueError, match="intent_id and mutation_id are required"):
        register(store, mutation_id="")
    with pytest.raises(ValueError, match="broker order lineage identity is required"):
        register(store, predecessor="")
    with pytest.raises(ValueError, match="broker order lineage identity is required"):
        register(store, successor="")


def test_postgres_lineage_requires_exact_successful_replace_proof(monkeypatch) -> None:
    for bad_proof in (
        None,
        proof(intent_id="other"),
        proof(kind="CANCEL"),
        proof(state="STARTED"),
        proof(successor="broker-X"),
        proof(predecessor="broker-X"),
    ):
        store, _ = store_with(monkeypatch, [Step(one=bad_proof)])
        with pytest.raises(ValueError, match="REPLACE_LINEAGE_NOT_PROVEN"):
            register(store)


def test_postgres_lineage_bootstraps_missing_root_and_inserts_successor(monkeypatch) -> None:
    store, connection = store_with(
        monkeypatch,
        [
            Step(one=proof()),
            Step(one=None),
            Step(one={"broker_order_id": "broker-A", "updated_at": NOW}),
            Step(one=None),
            Step(),
            Step(one=None),
            Step(),
        ],
    )

    register(store)

    assert connection.cursor_value.steps == []
    insert_sql = [
        sql
        for sql, _ in connection.cursor_value.executed
        if "INSERT INTO astra_broker_order_identities" in sql
    ]
    assert len(insert_sql) == 2


def test_postgres_lineage_rejects_unproven_predecessor_owner(monkeypatch) -> None:
    store, _ = store_with(
        monkeypatch,
        [Step(one=proof()), Step(one={"intent_id": "other-intent", "generation": 0})],
    )
    with pytest.raises(ValueError, match="REPLACE_PREDECESSOR_NOT_PROVEN"):
        register(store)

    store, _ = store_with(
        monkeypatch,
        [
            Step(one=proof()),
            Step(one=None),
            Step(one={"broker_order_id": "different", "updated_at": NOW}),
        ],
    )
    with pytest.raises(ValueError, match="REPLACE_PREDECESSOR_NOT_PROVEN"):
        register(store)

    store, _ = store_with(
        monkeypatch,
        [
            Step(one=proof()),
            Step(one=None),
            Step(one={"broker_order_id": "broker-A", "updated_at": NOW}),
            Step(one={"broker_order_id": "different-root"}),
        ],
    )
    with pytest.raises(ValueError, match="REPLACE_PREDECESSOR_NOT_PROVEN"):
        register(store)


def test_postgres_same_id_replace_returns_without_fake_successor(monkeypatch) -> None:
    store, connection = store_with(
        monkeypatch,
        [
            Step(one=proof(successor="broker-A")),
            Step(one={"intent_id": "intent-1", "generation": 0}),
        ],
    )
    register(store, successor="broker-A")
    assert connection.cursor_value.steps == []


def test_postgres_exact_successor_replay_is_idempotent(monkeypatch) -> None:
    store, connection = store_with(
        monkeypatch,
        [
            Step(one=proof()),
            Step(one={"intent_id": "intent-1", "generation": 0}),
            Step(
                one={
                    "intent_id": "intent-1",
                    "predecessor_broker_order_id": "broker-A",
                    "replace_mutation_id": "replace-1",
                    "generation": 1,
                }
            ),
        ],
    )
    register(store)
    assert connection.cursor_value.steps == []


def test_postgres_conflicting_existing_successor_fails_closed(monkeypatch) -> None:
    store, _ = store_with(
        monkeypatch,
        [
            Step(one=proof()),
            Step(one={"intent_id": "intent-1", "generation": 0}),
            Step(
                one={
                    "intent_id": "intent-1",
                    "predecessor_broker_order_id": "broker-X",
                    "replace_mutation_id": "other-mutation",
                    "generation": 7,
                }
            ),
        ],
    )
    with pytest.raises(ValueError, match="BROKER_ORDER_LINEAGE_CONFLICT"):
        register(store)


def test_postgres_existing_source_inserts_next_generation(monkeypatch) -> None:
    store, connection = store_with(
        monkeypatch,
        [
            Step(one=proof(predecessor="broker-B", successor="broker-C")),
            Step(one={"intent_id": "intent-1", "generation": 1}),
            Step(one=None),
            Step(),
        ],
    )
    register(store, predecessor="broker-B", successor="broker-C")
    sql, params = connection.cursor_value.executed[-1]
    assert "INSERT INTO astra_broker_order_identities" in sql
    assert params[4] == 2


def test_postgres_migrate_dispatches_base_and_lineage_scripts(monkeypatch) -> None:
    calls: list[object] = []

    def fake_base_migrate(self, path="migrations/product/001_durable_oms.sql"):
        calls.append(path)

    monkeypatch.setattr(PostgresOmsStore, "migrate", fake_base_migrate)
    store = IndexedPostgresOmsStore("postgresql://offline-unit")

    store.migrate(Path("custom.sql"))
    assert calls == [Path("custom.sql")]

    connection = FakeConnection([Step()])
    monkeypatch.setattr(store, "_connect", lambda: connection)
    store.migrate()
    assert calls[-1] == "migrations/product/001_durable_oms.sql"
    assert connection.commits == 1
    sql = connection.cursor_value.executed[0][0]
    assert "CREATE TABLE IF NOT EXISTS astra_broker_order_identities" in sql
