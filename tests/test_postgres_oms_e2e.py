from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL OMS integration tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.execution.paper_executor import PaperSubmitExecutor
from app.oms.postgres import PostgresOmsStore
from app.oms.store import OrderState
from app.risk.pretrade import (
    PreTradeRiskEngine,
    RiskDecision,
    RiskEvaluationMode,
    RiskLimits,
)
from app.runtime.paper_broker_contract_v99 import BrokerOrder, BrokerOrderStatus, OrderSide

NOW = datetime(2026, 8, 7, 14, 0, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="pg-intent-1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="pg-validation",
    )


def decision(value: OrderIntent) -> RiskDecision:
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("10000"),
            maximum_symbol_notional=Decimal("10000"),
            maximum_gross_notional=Decimal("10000"),
        )
    ).evaluate(
        value,
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        mode=RiskEvaluationMode.REPLAY,
    )


class BlockingPaperBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.submit_calls = 0
        self.get_calls = 0
        self.submit_entered = Event()
        self.release_submit = Event()
        self.orders: dict[str, BrokerOrder] = {}

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_entered.set()
        if not self.release_submit.wait(timeout=5):
            raise RuntimeError("test submit release timed out")
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="pg-broker-race-1",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_quantity=Decimal("0"),
            updated_at=NOW,
        )
        self.orders[order.client_order_id] = order
        return order

    def get_order_by_client_order_id(self, client_order_id: str):
        self.get_calls += 1
        return self.orders.get(client_order_id)


class PriceDriftPaperBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.submit_calls = 0

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        return BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="pg-broker-price-drift",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=Decimal("150"),
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_quantity=Decimal("0"),
            updated_at=NOW,
        )

    def get_order_by_client_order_id(self, client_order_id: str):
        return None


@pytest.fixture()
def store() -> PostgresOmsStore:
    value = PostgresOmsStore(DSN)
    value.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE astra_oms_outbox, astra_oms_events, astra_oms_orders RESTART IDENTITY CASCADE"
        )
    return value


def test_postgres_order_lifecycle_is_durable_and_idempotent(store: PostgresOmsStore) -> None:
    lifecycle = PaperOrderLifecycle(store)
    value = intent()
    prepared = lifecycle.prepare(value, decision(value), occurred_at=NOW)
    assert prepared.record.state is OrderState.OUTBOXED
    assert len(store.pending_outbox()) == 1

    repeated = lifecycle.prepare(value, decision(value), occurred_at=NOW)
    assert repeated.record.state is OrderState.OUTBOXED
    assert len(store.pending_outbox()) == 1

    message = store.pending_outbox()[0]
    store.mark_outbox_published(message.message_id, occurred_at=NOW)
    assert store.pending_outbox() == ()

    store.transition(
        "pg-intent-1",
        OrderState.SUBMIT_STARTED,
        event_id="pg-submit",
        occurred_at=NOW,
    )
    store.transition(
        "pg-intent-1",
        OrderState.ACKNOWLEDGED,
        event_id="pg-ack",
        occurred_at=NOW,
        broker_order_id="pg-broker-1",
    )
    partial = store.apply_cumulative_fill(
        "pg-intent-1",
        event_id="pg-fill-1",
        cumulative_filled=Decimal("4"),
        occurred_at=NOW,
    )
    assert partial.state is OrderState.PARTIALLY_FILLED

    reopened = PostgresOmsStore(DSN)
    persisted = reopened.get("pg-intent-1")
    assert persisted is not None
    assert persisted.state is OrderState.PARTIALLY_FILLED
    assert persisted.filled_quantity == Decimal("4")
    assert persisted.broker_order_id == "pg-broker-1"


def test_postgres_operational_blocking_count_tracks_uncertain_and_manual(
    store: PostgresOmsStore,
) -> None:
    value = intent()
    PaperOrderLifecycle(store).prepare(value, decision(value), occurred_at=NOW)
    assert store.operational_blocking_count() == 0

    store.transition(
        value.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="blocking-submit-started",
        occurred_at=NOW,
    )
    store.transition(
        value.intent_id,
        OrderState.UNCERTAIN,
        event_id="blocking-uncertain",
        occurred_at=NOW,
    )
    assert store.operational_blocking_count() == 1

    store.transition(
        value.intent_id,
        OrderState.RECONCILING,
        event_id="blocking-reconciling",
        occurred_at=NOW,
    )
    assert store.operational_blocking_count() == 1

    store.transition(
        value.intent_id,
        OrderState.MANUAL,
        event_id="blocking-manual",
        occurred_at=NOW,
    )
    assert store.operational_blocking_count() == 1


def test_postgres_row_lock_and_event_key_make_duplicate_fill_at_most_once(
    store: PostgresOmsStore,
) -> None:
    lifecycle = PaperOrderLifecycle(store)
    value = intent()
    lifecycle.prepare(value, decision(value), occurred_at=NOW)
    store.transition(
        "pg-intent-1",
        OrderState.SUBMIT_STARTED,
        event_id="submit",
        occurred_at=NOW,
    )
    store.transition(
        "pg-intent-1",
        OrderState.ACKNOWLEDGED,
        event_id="ack",
        occurred_at=NOW,
        broker_order_id="pg-broker-1",
    )

    def apply_once():
        worker = PostgresOmsStore(DSN)
        return worker.apply_cumulative_fill(
            "pg-intent-1",
            event_id="shared-broker-event",
            cumulative_filled=Decimal("5"),
            occurred_at=NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: apply_once(), range(2)))
    assert all(result.filled_quantity == Decimal("5") for result in results)
    events = [
        event
        for event in store.events("pg-intent-1")
        if event["event_id"] == "shared-broker-event"
    ]
    assert len(events) == 1
    persisted = store.get("pg-intent-1")
    assert persisted is not None and persisted.version == 6


def test_postgres_submit_claim_allows_one_worker_only(store: PostgresOmsStore) -> None:
    value = intent()
    PaperOrderLifecycle(store).prepare(value, decision(value), occurred_at=NOW)
    message = store.pending_outbox()[0]
    broker = BlockingPaperBroker()
    winner = PaperSubmitExecutor(store=PostgresOmsStore(DSN), broker=broker)
    contender = PaperSubmitExecutor(store=PostgresOmsStore(DSN), broker=broker)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(winner.execute, message, occurred_at=NOW)
        assert broker.submit_entered.wait(timeout=5)
        second = executor.submit(
            contender.execute,
            message,
            occurred_at=NOW + timedelta(seconds=1),
        )
        second_result = second.result(timeout=5)
        assert second_result.record.state is OrderState.SUBMIT_STARTED
        assert not second_result.mutation_attempted
        assert not second_result.recovered_by_read
        assert broker.submit_calls == 0
        assert broker.get_calls == 0

        broker.release_submit.set()
        first_result = first.result(timeout=5)

    assert first_result.record.state is OrderState.ACKNOWLEDGED
    assert first_result.mutation_attempted
    assert broker.submit_calls == 1
    assert broker.get_calls == 0
    assert store.pending_outbox() == ()

    persisted = store.get("pg-intent-1")
    assert persisted is not None
    assert persisted.state is OrderState.ACKNOWLEDGED
    assert persisted.broker_order_id == "pg-broker-race-1"


def test_postgres_f17_price_drift_is_durable_uncertain_not_acknowledged(
    store: PostgresOmsStore,
) -> None:
    value = intent()
    PaperOrderLifecycle(store).prepare(value, decision(value), occurred_at=NOW)
    message = store.pending_outbox()[0]
    broker = PriceDriftPaperBroker()

    result = PaperSubmitExecutor(store=PostgresOmsStore(DSN), broker=broker).execute(
        message,
        occurred_at=NOW,
    )

    assert result.record.state is OrderState.UNCERTAIN
    assert result.record.broker_order_id == ""
    assert result.record.filled_quantity == Decimal("0")
    assert broker.submit_calls == 1
    assert store.pending_outbox() == ()
    events = [event for event in store.events(value.intent_id) if event["event_type"] == "UNCERTAIN"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["reason"] == "BROKER_SUBMIT_ECONOMICS_MISMATCH"
    assert "limit_price" in payload["mismatches"]
    assert payload["authorized"]["limit_price"] == "100"
    assert payload["broker_response"]["limit_price"] == "150"


def test_postgres_event_journal_is_append_only(store: PostgresOmsStore) -> None:
    value = intent()
    PaperOrderLifecycle(store).prepare(value, decision(value), occurred_at=NOW)
    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_oms_events SET event_type='TAMPERED' WHERE event_id=%s",
                ("create:pg-intent-1",),
            )
        connection.rollback()
    assert store.events("pg-intent-1")[0]["event_type"] == "CREATED"


@pytest.mark.parametrize(
    "changed",
    (
        lambda value: replace(value, symbol="MSFT"),
        lambda value: replace(value, side=Side.SELL),
        lambda value: replace(value, quantity=Decimal("9")),
        lambda value: replace(value, limit_price=Decimal("999")),
        lambda value: replace(value, strategy_id="other-strategy"),
        lambda value: replace(value, created_at=value.created_at + timedelta(seconds=1)),
    ),
)
def test_postgres_f20_same_intent_id_with_changed_economics_conflicts(
    store: PostgresOmsStore,
    changed,
) -> None:
    original = intent()
    first = store.create(original, client_order_id="pg-f20-client", occurred_at=NOW)

    with pytest.raises(ValueError, match="INTENT_ID_CONFLICT"):
        store.create(
            changed(original),
            client_order_id="pg-f20-client",
            occurred_at=NOW,
        )

    assert store.get(original.intent_id) == first


def test_postgres_f20_different_intent_cannot_alias_client_order_id(
    store: PostgresOmsStore,
) -> None:
    original = intent()
    store.create(original, client_order_id="pg-shared-client", occurred_at=NOW)
    alias = replace(original, intent_id="pg-intent-alias")

    with pytest.raises(ValueError, match="INTENT_ID_CONFLICT"):
        store.create(alias, client_order_id="pg-shared-client", occurred_at=NOW)

    assert store.get(original.intent_id) is not None
    assert store.get(alias.intent_id) is None


def test_postgres_f20_event_id_binds_target_payload_and_broker_identity(
    store: PostgresOmsStore,
) -> None:
    value = intent()
    store.create(value, client_order_id="pg-f20-client", occurred_at=NOW)
    store.transition(value.intent_id, OrderState.RISK_APPROVED, event_id="risk", occurred_at=NOW)
    store.enqueue_submit(value.intent_id, event_id="outbox", occurred_at=NOW)
    store.transition(
        value.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="submit-started",
        occurred_at=NOW,
    )
    first = store.transition(
        value.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="shared-event",
        occurred_at=NOW,
        broker_order_id="broker-A",
        payload={"source": "submit"},
    )
    exact = store.transition(
        value.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="shared-event",
        occurred_at=NOW + timedelta(seconds=1),
        broker_order_id="broker-A",
        payload={"source": "submit"},
    )
    assert exact == first

    for target, payload, broker_id in (
        (OrderState.UNCERTAIN, {"source": "submit"}, "broker-A"),
        (OrderState.ACKNOWLEDGED, {"source": "reconcile"}, "broker-A"),
        (OrderState.ACKNOWLEDGED, {"source": "submit"}, "broker-B"),
    ):
        with pytest.raises(ValueError, match="OMS_EVENT_ID_CONFLICT"):
            store.transition(
                value.intent_id,
                target,
                event_id="shared-event",
                occurred_at=NOW + timedelta(seconds=2),
                broker_order_id=broker_id,
                payload=payload,
            )

    assert store.get(value.intent_id) == first


def test_postgres_f20_fill_event_id_cannot_hide_changed_economics(
    store: PostgresOmsStore,
) -> None:
    value = intent()
    store.create(value, client_order_id="pg-f20-client", occurred_at=NOW)
    store.transition(value.intent_id, OrderState.RISK_APPROVED, event_id="risk", occurred_at=NOW)
    store.enqueue_submit(value.intent_id, event_id="outbox", occurred_at=NOW)
    store.transition(
        value.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="submit-started",
        occurred_at=NOW,
    )
    store.transition(
        value.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="ack",
        occurred_at=NOW,
        broker_order_id="broker-A",
    )
    first = store.apply_cumulative_fill(
        value.intent_id,
        event_id="fill-1",
        cumulative_filled=Decimal("4"),
        occurred_at=NOW,
        broker_order_id="broker-A",
    )
    exact = store.apply_cumulative_fill(
        value.intent_id,
        event_id="fill-1",
        cumulative_filled=Decimal("4"),
        occurred_at=NOW + timedelta(seconds=1),
        broker_order_id="broker-A",
    )
    assert exact == first

    with pytest.raises(ValueError, match="OMS_EVENT_ID_CONFLICT"):
        store.apply_cumulative_fill(
            value.intent_id,
            event_id="fill-1",
            cumulative_filled=Decimal("5"),
            occurred_at=NOW + timedelta(seconds=2),
            broker_order_id="broker-A",
        )
    with pytest.raises(ValueError, match="OMS_EVENT_ID_CONFLICT"):
        store.apply_cumulative_fill(
            value.intent_id,
            event_id="fill-1",
            cumulative_filled=Decimal("4"),
            occurred_at=NOW + timedelta(seconds=2),
            broker_order_id="broker-B",
        )

    assert store.get(value.intent_id) == first


def test_postgres_f20_concurrent_divergent_intent_replay_has_one_winner(
    store: PostgresOmsStore,
) -> None:
    original = intent()
    divergent = replace(original, limit_price=Decimal("101"))

    def create(value: OrderIntent):
        worker = PostgresOmsStore(DSN)
        try:
            return ("ok", worker.create(value, client_order_id="pg-f20-client", occurred_at=NOW))
        except ValueError as exc:
            return ("conflict", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, (original, divergent)))

    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    assert any(result == ("conflict", "INTENT_ID_CONFLICT") for result in results)
    persisted = store.get(original.intent_id)
    assert persisted is not None
    assert persisted.limit_price in {Decimal("100"), Decimal("101")}
