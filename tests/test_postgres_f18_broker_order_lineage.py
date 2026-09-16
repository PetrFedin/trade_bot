from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL F18 lineage tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.oms.indexed import IndexedPostgresOmsStore
from app.oms.order_mutations import OrderMutationLifecycle
from app.oms.order_mutations_postgres import PostgresOrderMutationStore
from app.oms.store import OrderState
from app.risk.pretrade import (
    PreTradeRiskEngine,
    RiskEvaluationMode,
    RiskLimits,
)

NOW = datetime(2026, 9, 16, 19, 0, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="pg-f18-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="pg-f18-lineage",
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


@pytest.fixture()
def stores():
    oms = IndexedPostgresOmsStore(DSN)
    oms.migrate()
    mutations = PostgresOrderMutationStore(DSN)
    mutations.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """TRUNCATE astra_broker_order_identities,
            astra_order_mutation_outbox, astra_order_mutation_events,
            astra_order_mutations, astra_oms_outbox, astra_oms_events,
            astra_oms_orders RESTART IDENTITY CASCADE"""
        )
    value = intent()
    PaperOrderLifecycle(oms).prepare(value, approved(value), occurred_at=NOW)
    oms.transition(
        value.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="pg-f18-submit-started",
        occurred_at=NOW,
    )
    oms.transition(
        value.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="pg-f18-ack",
        occurred_at=NOW,
        broker_order_id="pg-broker-A",
    )
    return oms, mutations


def prove_replace(
    oms: IndexedPostgresOmsStore,
    mutations: PostgresOrderMutationStore,
    *,
    mutation_id: str,
    target: str,
    successor: str,
) -> tuple[str, str]:
    lifecycle = OrderMutationLifecycle(oms=oms, mutations=mutations)
    requested = lifecycle.request_replace(
        "pg-f18-intent",
        mutation_id=mutation_id,
        target_limit_price=Decimal(target),
        occurred_at=NOW,
    )
    predecessor = requested.broker_order_id
    mutations.mark_started(mutation_id, occurred_at=NOW)
    mutations.mark_succeeded(
        mutation_id,
        outcome="REPLACED",
        occurred_at=NOW,
        broker_order_id=successor,
    )
    oms.register_replace_successor(
        intent_id="pg-f18-intent",
        mutation_id=mutation_id,
        predecessor_broker_order_id=predecessor,
        successor_broker_order_id=successor,
        occurred_at=NOW,
    )
    return predecessor, successor


def test_postgres_lineage_resolves_original_and_successors(stores) -> None:
    oms, mutations = stores
    prove_replace(
        oms,
        mutations,
        mutation_id="pg-replace-A-B",
        target="101",
        successor="pg-broker-B",
    )
    prove_replace(
        oms,
        mutations,
        mutation_id="pg-replace-B-C",
        target="102",
        successor="pg-broker-C",
    )

    for broker_id in ("pg-broker-A", "pg-broker-B", "pg-broker-C"):
        resolved = oms.get_by_broker_order_id(broker_id)
        assert resolved is not None
        assert resolved.intent_id == "pg-f18-intent"

    with psycopg.connect(DSN) as connection:
        rows = connection.execute(
            """SELECT broker_order_id, predecessor_broker_order_id,
                      replace_mutation_id, generation
            FROM astra_broker_order_identities
            WHERE intent_id=%s ORDER BY generation""",
            ("pg-f18-intent",),
        ).fetchall()
    assert rows == [
        ("pg-broker-A", None, None, 0),
        ("pg-broker-B", "pg-broker-A", "pg-replace-A-B", 1),
        ("pg-broker-C", "pg-broker-B", "pg-replace-B-C", 2),
    ]


def test_postgres_same_successor_registration_is_concurrently_idempotent(stores) -> None:
    oms, mutations = stores
    lifecycle = OrderMutationLifecycle(oms=oms, mutations=mutations)
    requested = lifecycle.request_replace(
        "pg-f18-intent",
        mutation_id="pg-replace-race",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    mutations.mark_started("pg-replace-race", occurred_at=NOW)
    mutations.mark_succeeded(
        "pg-replace-race",
        outcome="REPLACED",
        occurred_at=NOW,
        broker_order_id="pg-broker-B",
    )

    def register_once(_: int) -> None:
        IndexedPostgresOmsStore(DSN).register_replace_successor(
            intent_id="pg-f18-intent",
            mutation_id="pg-replace-race",
            predecessor_broker_order_id=requested.broker_order_id,
            successor_broker_order_id="pg-broker-B",
            occurred_at=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(register_once, range(2)))

    with psycopg.connect(DSN) as connection:
        count = connection.execute(
            """SELECT count(*) FROM astra_broker_order_identities
            WHERE broker_order_id='pg-broker-B'"""
        ).fetchone()[0]
    assert count == 1


def test_postgres_lineage_is_append_only(stores) -> None:
    oms, mutations = stores
    prove_replace(
        oms,
        mutations,
        mutation_id="pg-replace-A-B",
        target="101",
        successor="pg-broker-B",
    )

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute(
                """UPDATE astra_broker_order_identities SET generation=9
                WHERE broker_order_id='pg-broker-B'"""
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute(
                """DELETE FROM astra_broker_order_identities
                WHERE broker_order_id='pg-broker-B'"""
            )


def test_postgres_unproven_successor_is_rejected(stores) -> None:
    oms, _ = stores
    with pytest.raises(ValueError, match="REPLACE_LINEAGE_NOT_PROVEN"):
        oms.register_replace_successor(
            intent_id="pg-f18-intent",
            mutation_id="not-a-mutation",
            predecessor_broker_order_id="pg-broker-A",
            successor_broker_order_id="pg-broker-X",
            occurred_at=NOW,
        )
