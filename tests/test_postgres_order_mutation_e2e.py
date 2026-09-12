from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.execution.order_mutation_executor import PaperOrderMutationExecutor
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
from app.runtime.paper_broker_contract_v99 import BrokerOrder, BrokerOrderStatus, OrderSide

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


class BlockingMutationBroker:
    paper_order_writes_enabled = True

    def __init__(self, client_order_id: str) -> None:
        self.cancel_calls = 0
        self.replace_calls = 0
        self.get_calls = 0
        self.cancel_entered = Event()
        self.replace_entered = Event()
        self.release_cancel = Event()
        self.release_replace = Event()
        self.order = BrokerOrder(
            client_order_id=client_order_id,
            broker_order_id="pg-broker-1",
            instrument="AAPL",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            limit_price=Decimal("100"),
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_quantity=Decimal("0"),
            updated_at=NOW,
        )

    def cancel_order(self, *, broker_order_id: str) -> BrokerOrder:
        if broker_order_id != self.order.broker_order_id:
            raise AssertionError("unexpected broker order id")
        self.cancel_entered.set()
        if not self.release_cancel.wait(timeout=5):
            raise RuntimeError("test cancel release timed out")
        self.cancel_calls += 1
        self.order = replace(
            self.order,
            status=BrokerOrderStatus.CANCELLED,
            updated_at=NOW,
        )
        return self.order

    def replace_limit_order(
        self, *, broker_order_id: str, limit_price: Decimal
    ) -> BrokerOrder:
        if broker_order_id != self.order.broker_order_id:
            raise AssertionError("unexpected broker order id")
        self.replace_entered.set()
        if not self.release_replace.wait(timeout=5):
            raise RuntimeError("test replace release timed out")
        self.replace_calls += 1
        self.order = replace(
            self.order,
            broker_order_id="pg-broker-2",
            limit_price=limit_price,
            status=BrokerOrderStatus.REPLACED,
            updated_at=NOW,
        )
        return self.order

    def get_order_by_client_order_id(self, client_order_id: str):
        self.get_calls += 1
        return self.order if client_order_id == self.order.client_order_id else None


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


def test_postgres_cancel_claim_grants_one_external_delete(stores) -> None:
    oms, mutations = stores
    OrderMutationLifecycle(oms=oms, mutations=mutations).request_cancel(
        "pg-mutation-intent", mutation_id="pg-cancel-race", occurred_at=NOW
    )
    message = mutations.pending_outbox()[0]
    order = oms.get("pg-mutation-intent")
    assert order is not None
    broker = BlockingMutationBroker(order.client_order_id)
    first = PaperOrderMutationExecutor(
        oms=PostgresOmsStore(DSN),
        mutations=PostgresOrderMutationStore(DSN),
        broker=broker,
    )
    second = PaperOrderMutationExecutor(
        oms=PostgresOmsStore(DSN),
        mutations=PostgresOrderMutationStore(DSN),
        broker=broker,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(first.execute, message, occurred_at=NOW)
        assert broker.cancel_entered.wait(timeout=5)
        contender = pool.submit(
            second.execute,
            message,
            occurred_at=NOW + timedelta(seconds=1),
        )
        contender_result = contender.result(timeout=5)
        assert contender_result.mutation.state is MutationState.STARTED
        assert not contender_result.mutation_attempted
        assert not contender_result.recovered_by_read
        assert broker.cancel_calls == 0
        assert broker.get_calls == 0
        broker.release_cancel.set()
        winner_result = winner.result(timeout=5)

    assert winner_result.mutation.state is MutationState.SUCCEEDED
    assert winner_result.record.state is OrderState.CANCELLED
    assert broker.cancel_calls == 1
    assert broker.get_calls == 0


def test_postgres_replace_claim_grants_one_external_patch(stores) -> None:
    oms, mutations = stores
    OrderMutationLifecycle(oms=oms, mutations=mutations).request_replace(
        "pg-mutation-intent",
        mutation_id="pg-replace-race",
        target_limit_price=Decimal("101"),
        occurred_at=NOW,
    )
    message = mutations.pending_outbox()[0]
    order = oms.get("pg-mutation-intent")
    assert order is not None
    broker = BlockingMutationBroker(order.client_order_id)
    first = PaperOrderMutationExecutor(
        oms=PostgresOmsStore(DSN),
        mutations=PostgresOrderMutationStore(DSN),
        broker=broker,
    )
    second = PaperOrderMutationExecutor(
        oms=PostgresOmsStore(DSN),
        mutations=PostgresOrderMutationStore(DSN),
        broker=broker,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(first.execute, message, occurred_at=NOW)
        assert broker.replace_entered.wait(timeout=5)
        contender = pool.submit(
            second.execute,
            message,
            occurred_at=NOW + timedelta(seconds=1),
        )
        contender_result = contender.result(timeout=5)
        assert contender_result.mutation.state is MutationState.STARTED
        assert not contender_result.mutation_attempted
        assert not contender_result.recovered_by_read
        assert broker.replace_calls == 0
        assert broker.get_calls == 0
        broker.release_replace.set()
        winner_result = winner.result(timeout=5)

    assert winner_result.mutation.state is MutationState.SUCCEEDED
    assert winner_result.mutation.outcome == "REPLACED"
    assert broker.replace_calls == 1
    assert broker.get_calls == 0


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
