from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.oms.order_mutations import (
    ActiveMutationExists,
    MutationKind,
    MutationState,
    OrderMutationLifecycle,
)
from app.oms.order_mutations_postgres import PostgresOrderMutationStore
from app.oms.postgres import PostgresOmsStore
from app.oms.store import OrderState
from app.risk.pretrade import RiskDecision

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL order-mutation tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

NOW = datetime(2026, 8, 9, 19, 0, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="pg-mutation-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="pg-mutation-e2e",
    )


def approved() -> RiskDecision:
    return RiskDecision(True, (), Decimal("1000"), Decimal("1000"), Decimal("1000"))


@pytest.fixture()
def stores():
    oms = PostgresOmsStore(DSN)
    oms.migrate()
    mutations = PostgresOrderMutationStore(DSN)
    mutations.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """TRUNCATE astra_order_mutation_outbox, astra_order_mutation_events,
            astra_order_mutations, astra_oms_outbox, astra_oms_events, astra_oms_orders
            RESTART IDENTITY CASCADE"""
        )
    PaperOrderLifecycle(oms).prepare(intent(), approved(), occurred_at=NOW)
    message = oms.pending_outbox()[0]
    oms.mark_outbox_published(message.message_id, occurred_at=NOW)
    oms.transition(
        "pg-mutation-intent",
        OrderState.SUBMIT_STARTED,
        event_id="pg-mut-submit",
        occurred_at=NOW,
    )
    oms.transition(
        "pg-mutation-intent",
        OrderState.ACKNOWLEDGED,
        event_id="pg-mut-ack",
        occurred_at=NOW,
        broker_order_id="pg-broker-1",
    )
    return oms, mutations


def test_postgres_mutation_journal_is_durable_and_tracks_replacement(stores) -> None:
    oms, mutations = stores
    lifecycle = OrderMutationLifecycle(oms=oms, mutations=mutations)
    requested = lifecycle.request_replace(
        "pg-mutation-intent",
        mutation_id="pg-replace-1",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    assert requested.state is MutationState.REQUESTED
    assert len(mutations.pending_outbox()) == 1
    mutations.mark_started("pg-replace-1", occurred_at=NOW)
    succeeded = mutations.mark_succeeded(
        "pg-replace-1",
        outcome="REPLACED",
        occurred_at=NOW,
        broker_order_id="pg-broker-2",
    )
    assert succeeded.state is MutationState.SUCCEEDED

    reopened = PostgresOrderMutationStore(DSN)
    assert reopened.current_limit_price(
        "pg-mutation-intent", fallback=Decimal("100")
    ) == Decimal("101")
    assert reopened.current_broker_order_id(
        "pg-mutation-intent", fallback="pg-broker-1"
    ) == "pg-broker-2"
    cancel = OrderMutationLifecycle(oms=oms, mutations=reopened).request_cancel(
        "pg-mutation-intent", mutation_id="pg-cancel-1", occurred_at=NOW
    )
    assert cancel.kind is MutationKind.CANCEL
    assert cancel.broker_order_id == "pg-broker-2"


def test_postgres_partial_unique_index_fences_concurrent_mutations(stores) -> None:
    oms, _ = stores

    def request(index: int):
        mutations = PostgresOrderMutationStore(DSN)
        lifecycle = OrderMutationLifecycle(oms=PostgresOmsStore(DSN), mutations=mutations)
        try:
            if index == 0:
                return lifecycle.request_cancel(
                    "pg-mutation-intent",
                    mutation_id="pg-fence-cancel",
                    occurred_at=NOW,
                ).mutation_id
            return lifecycle.request_replace(
                "pg-mutation-intent",
                mutation_id="pg-fence-replace",
                target_limit_price=Decimal("101"),
                occurred_at=NOW,
            ).mutation_id
        except ActiveMutationExists:
            return "FENCED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(request, range(2)))
    assert results.count("FENCED") == 1
    assert len([value for value in results if value != "FENCED"]) == 1
    persisted = oms.get("pg-mutation-intent")
    assert persisted is not None and persisted.state is OrderState.ACKNOWLEDGED


def test_postgres_mutation_event_journal_is_append_only(stores) -> None:
    oms, mutations = stores
    OrderMutationLifecycle(oms=oms, mutations=mutations).request_cancel(
        "pg-mutation-intent", mutation_id="pg-journal", occurred_at=NOW
    )
    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_order_mutation_events SET event_type='TAMPERED'"
            )
        connection.rollback()
    assert mutations.events("pg-journal")[0]["event_type"] == "REQUESTED"
