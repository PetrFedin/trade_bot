from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.oms.indexed import IndexedDurableOmsStore
from app.oms.order_mutations import DurableOrderMutationStore, OrderMutationLifecycle
from app.oms.store import DurableOmsStore, OrderState
from app.risk.pretrade import (
    PreTradeRiskEngine,
    RiskEvaluationMode,
    RiskLimits,
)

NOW = datetime(2026, 9, 16, 20, 0, tzinfo=UTC)


def make_intent(intent_id: str, symbol: str = "AAPL") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="f18-edges",
    )


def approved(value: OrderIntent):
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("10000"),
            maximum_symbol_notional=Decimal("10000"),
            maximum_gross_notional=Decimal("10000"),
        )
    ).evaluate(
        value,
        mode=RiskEvaluationMode.REPLAY,
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
    )


def create_ack(store: DurableOmsStore, value: OrderIntent, broker_id: str) -> None:
    PaperOrderLifecycle(store).prepare(value, approved(value), occurred_at=NOW)
    store.transition(
        value.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id=f"submit:{value.intent_id}",
        occurred_at=NOW,
    )
    store.transition(
        value.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id=f"ack:{value.intent_id}",
        occurred_at=NOW,
        broker_order_id=broker_id,
    )


def lineage_db(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "lineage-edges.sqlite"
    oms = IndexedDurableOmsStore(db)
    create_ack(oms, make_intent("edge-intent"), "broker-A")
    mutations = DurableOrderMutationStore(db)
    lifecycle = OrderMutationLifecycle(oms=oms, mutations=mutations)
    return db, oms, mutations, lifecycle


def successful_replace(
    oms: IndexedDurableOmsStore,
    mutations: DurableOrderMutationStore,
    lifecycle: OrderMutationLifecycle,
    *,
    mutation_id: str,
    target: str,
    successor: str,
    occurred_at: datetime = NOW,
) -> str:
    requested = lifecycle.request_replace(
        "edge-intent",
        mutation_id=mutation_id,
        target_limit_price=Decimal(target),
        occurred_at=occurred_at,
    )
    predecessor = requested.broker_order_id
    mutations.mark_started(mutation_id, occurred_at=occurred_at)
    mutations.mark_succeeded(
        mutation_id,
        outcome="REPLACED",
        occurred_at=occurred_at,
        broker_order_id=successor,
    )
    oms.register_replace_successor(
        intent_id="edge-intent",
        mutation_id=mutation_id,
        predecessor_broker_order_id=predecessor,
        successor_broker_order_id=successor,
        occurred_at=occurred_at,
    )
    return predecessor


def test_reopen_existing_lineage_is_idempotent_and_lookup_contract_is_strict(tmp_path) -> None:
    db, oms, _, _ = lineage_db(tmp_path)
    reopened = IndexedDurableOmsStore(db)

    record = oms.get("edge-intent")
    assert record is not None
    assert reopened.get_by_client_order_id(record.client_order_id) == record
    assert reopened.get_by_client_order_id("missing-client") is None
    assert reopened.get_by_broker_order_id("broker-A") == record
    assert reopened.get_by_broker_order_id("missing-broker") is None

    with pytest.raises(ValueError, match="client_order_id is required"):
        reopened.get_by_client_order_id("   ")
    with pytest.raises(ValueError, match="broker_order_id is required"):
        reopened.get_by_broker_order_id("")


def test_opening_indexed_store_rejects_identity_owned_by_another_intent(tmp_path) -> None:
    db, _, _, _ = lineage_db(tmp_path)
    base = DurableOmsStore(db)
    create_ack(base, make_intent("second-intent", "MSFT"), "broker-B")

    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """INSERT INTO oms_broker_order_identities
            (broker_order_id, intent_id, predecessor_broker_order_id,
             replace_mutation_id, generation, created_at)
            VALUES ('broker-B', 'edge-intent', 'broker-A', 'manual-conflict', 1, ?)""",
            (NOW.isoformat(),),
        )

    with pytest.raises(ValueError, match="OMS_BROKER_ORDER_ID_CONFLICT"):
        IndexedDurableOmsStore(db)


def test_lookup_rejects_duplicate_legacy_broker_id_when_no_lineage_alias_exists(tmp_path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "duplicate-legacy.sqlite"
    indexed = IndexedDurableOmsStore(db)
    base = DurableOmsStore(db)
    create_ack(base, make_intent("dup-one", "AAPL"), "duplicate-id")
    create_ack(base, make_intent("dup-two", "MSFT"), "duplicate-id")

    with pytest.raises(ValueError, match="OMS_BROKER_ORDER_ID_CONFLICT"):
        indexed.get_by_broker_order_id("duplicate-id")


def test_lookup_detects_lineage_row_pointing_to_missing_order(tmp_path) -> None:
    db, oms, _, _ = lineage_db(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER oms_broker_order_identities_no_delete")
        connection.execute("DELETE FROM oms_orders WHERE intent_id='edge-intent'")

    with pytest.raises(RuntimeError, match="points to missing order"):
        oms.get_by_broker_order_id("broker-A")


def test_register_replace_validates_required_identity_fields(tmp_path) -> None:
    _, oms, _, _ = lineage_db(tmp_path)
    base = dict(
        intent_id="edge-intent",
        mutation_id="replace",
        predecessor_broker_order_id="broker-A",
        successor_broker_order_id="broker-B",
        occurred_at=NOW,
    )
    with pytest.raises(ValueError, match="intent_id and mutation_id are required"):
        oms.register_replace_successor(**{**base, "intent_id": ""})
    with pytest.raises(ValueError, match="intent_id and mutation_id are required"):
        oms.register_replace_successor(**{**base, "mutation_id": ""})
    with pytest.raises(ValueError, match="broker order lineage identity is required"):
        oms.register_replace_successor(
            **{**base, "predecessor_broker_order_id": ""}
        )
    with pytest.raises(ValueError, match="broker order lineage identity is required"):
        oms.register_replace_successor(**{**base, "successor_broker_order_id": ""})


def test_malformed_mutation_outbox_cannot_prove_lineage(tmp_path) -> None:
    db, oms, mutations, lifecycle = lineage_db(tmp_path)
    lifecycle.request_replace(
        "edge-intent",
        mutation_id="malformed-proof",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    mutations.mark_started("malformed-proof", occurred_at=NOW)
    mutations.mark_succeeded(
        "malformed-proof",
        outcome="REPLACED",
        occurred_at=NOW,
        broker_order_id="broker-B",
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            """UPDATE oms_order_mutation_outbox SET payload='not-json'
            WHERE mutation_id='malformed-proof'"""
        )

    with pytest.raises(ValueError, match="REPLACE_LINEAGE_NOT_PROVEN"):
        oms.register_replace_successor(
            intent_id="edge-intent",
            mutation_id="malformed-proof",
            predecessor_broker_order_id="broker-A",
            successor_broker_order_id="broker-B",
            occurred_at=NOW,
        )


def test_cancel_or_unfinished_mutation_cannot_prove_replace_lineage(tmp_path) -> None:
    _, oms, mutations, lifecycle = lineage_db(tmp_path)
    lifecycle.request_cancel(
        "edge-intent",
        mutation_id="cancel-proof",
        occurred_at=NOW,
    )
    mutations.mark_started("cancel-proof", occurred_at=NOW)
    mutations.mark_succeeded(
        "cancel-proof",
        outcome="CANCELLED",
        occurred_at=NOW,
        broker_order_id="broker-B",
    )
    with pytest.raises(ValueError, match="REPLACE_LINEAGE_NOT_PROVEN"):
        oms.register_replace_successor(
            intent_id="edge-intent",
            mutation_id="cancel-proof",
            predecessor_broker_order_id="broker-A",
            successor_broker_order_id="broker-B",
            occurred_at=NOW,
        )

    # A real replace that has not reached SUCCEEDED is equally insufficient.
    mutations.request(
        mutation_id="unfinished-replace",
        intent_id="edge-intent",
        kind="REPLACE",  # type: ignore[arg-type]
        target_limit_price=Decimal("101"),
        baseline_limit_price=Decimal("100"),
        broker_order_id="broker-A",
        occurred_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="REPLACE_LINEAGE_NOT_PROVEN"):
        oms.register_replace_successor(
            intent_id="edge-intent",
            mutation_id="unfinished-replace",
            predecessor_broker_order_id="broker-A",
            successor_broker_order_id="broker-B",
            occurred_at=NOW,
        )


def test_proof_must_match_intent_successor_and_predecessor(tmp_path) -> None:
    _, oms, mutations, lifecycle = lineage_db(tmp_path)
    lifecycle.request_replace(
        "edge-intent",
        mutation_id="exact-proof",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    mutations.mark_started("exact-proof", occurred_at=NOW)
    mutations.mark_succeeded(
        "exact-proof",
        outcome="REPLACED",
        occurred_at=NOW,
        broker_order_id="broker-B",
    )

    for kwargs in (
        {"intent_id": "other-intent"},
        {"successor_broker_order_id": "broker-C"},
        {"predecessor_broker_order_id": "broker-X"},
    ):
        arguments = dict(
            intent_id="edge-intent",
            mutation_id="exact-proof",
            predecessor_broker_order_id="broker-A",
            successor_broker_order_id="broker-B",
            occurred_at=NOW,
        )
        arguments.update(kwargs)
        with pytest.raises(ValueError, match="REPLACE_LINEAGE_NOT_PROVEN"):
            oms.register_replace_successor(**arguments)


def test_same_broker_id_replace_is_proven_but_creates_no_fake_generation(tmp_path) -> None:
    db, oms, mutations, lifecycle = lineage_db(tmp_path)
    lifecycle.request_replace(
        "edge-intent",
        mutation_id="replace-in-place",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    mutations.mark_started("replace-in-place", occurred_at=NOW)
    mutations.mark_succeeded(
        "replace-in-place",
        outcome="REPLACED",
        occurred_at=NOW,
        broker_order_id="broker-A",
    )

    oms.register_replace_successor(
        intent_id="edge-intent",
        mutation_id="replace-in-place",
        predecessor_broker_order_id="broker-A",
        successor_broker_order_id="broker-A",
        occurred_at=NOW,
    )

    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            """SELECT broker_order_id, generation FROM oms_broker_order_identities
            WHERE intent_id='edge-intent' ORDER BY generation"""
        ).fetchall()
    assert rows == [("broker-A", 0)]


def test_existing_successor_cannot_be_reused_for_different_lineage_edge(tmp_path) -> None:
    _, oms, mutations, lifecycle = lineage_db(tmp_path)
    successful_replace(
        oms,
        mutations,
        lifecycle,
        mutation_id="replace-A-B",
        target="101",
        successor="broker-B",
    )
    successful_replace(
        oms,
        mutations,
        lifecycle,
        mutation_id="replace-B-C",
        target="102",
        successor="broker-C",
        occurred_at=NOW + timedelta(seconds=1),
    )
    requested = lifecycle.request_replace(
        "edge-intent",
        mutation_id="replace-C-B",
        target_limit_price=Decimal("103"),
        occurred_at=NOW + timedelta(seconds=2),
    )
    assert requested.broker_order_id == "broker-C"
    mutations.mark_started("replace-C-B", occurred_at=NOW + timedelta(seconds=2))
    mutations.mark_succeeded(
        "replace-C-B",
        outcome="REPLACED",
        occurred_at=NOW + timedelta(seconds=2),
        broker_order_id="broker-B",
    )

    with pytest.raises(ValueError, match="BROKER_ORDER_LINEAGE_CONFLICT"):
        oms.register_replace_successor(
            intent_id="edge-intent",
            mutation_id="replace-C-B",
            predecessor_broker_order_id="broker-C",
            successor_broker_order_id="broker-B",
            occurred_at=NOW + timedelta(seconds=2),
        )


def test_second_successor_from_same_predecessor_fails_closed(tmp_path) -> None:
    db, oms, mutations, lifecycle = lineage_db(tmp_path)
    successful_replace(
        oms,
        mutations,
        lifecycle,
        mutation_id="replace-A-B",
        target="101",
        successor="broker-B",
    )

    # Build a second exact durable proof claiming the same predecessor A but a
    # different successor C. The unique predecessor constraint must reject the fork.
    mutations.request(
        mutation_id="replace-A-C",
        intent_id="edge-intent",
        kind="REPLACE",  # type: ignore[arg-type]
        target_limit_price=Decimal("102"),
        baseline_limit_price=Decimal("101"),
        broker_order_id="broker-A",
        occurred_at=NOW + timedelta(seconds=1),
    )
    mutations.mark_started("replace-A-C", occurred_at=NOW + timedelta(seconds=1))
    mutations.mark_succeeded(
        "replace-A-C",
        outcome="REPLACED",
        occurred_at=NOW + timedelta(seconds=1),
        broker_order_id="broker-C",
    )

    with pytest.raises(ValueError, match="BROKER_ORDER_LINEAGE_CONFLICT"):
        oms.register_replace_successor(
            intent_id="edge-intent",
            mutation_id="replace-A-C",
            predecessor_broker_order_id="broker-A",
            successor_broker_order_id="broker-C",
            occurred_at=NOW + timedelta(seconds=1),
        )

    with sqlite3.connect(db) as connection:
        successors = connection.execute(
            """SELECT broker_order_id FROM oms_broker_order_identities
            WHERE predecessor_broker_order_id='broker-A'"""
        ).fetchall()
    assert successors == [("broker-B",)]


def test_missing_or_wrong_predecessor_identity_fails_closed(tmp_path) -> None:
    db, oms, mutations, lifecycle = lineage_db(tmp_path)
    lifecycle.request_replace(
        "edge-intent",
        mutation_id="wrong-predecessor",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    mutations.mark_started("wrong-predecessor", occurred_at=NOW)
    mutations.mark_succeeded(
        "wrong-predecessor",
        outcome="REPLACED",
        occurred_at=NOW,
        broker_order_id="broker-B",
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            """UPDATE oms_order_mutation_outbox
            SET payload=json_set(payload, '$.broker_order_id', 'broker-X')
            WHERE mutation_id='wrong-predecessor'"""
        )

    with pytest.raises(ValueError, match="REPLACE_PREDECESSOR_NOT_PROVEN"):
        oms.register_replace_successor(
            intent_id="edge-intent",
            mutation_id="wrong-predecessor",
            predecessor_broker_order_id="broker-X",
            successor_broker_order_id="broker-B",
            occurred_at=NOW,
        )
