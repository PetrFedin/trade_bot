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
        "PostgreSQL OMS integration tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.oms.bybit_entry import PostgresBybitEntryOms, bybit_entry_intent_id
from app.oms.postgres import PostgresOmsStore
from app.oms.store import OrderState
from app.risk.pretrade import RiskDecision

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


def decision() -> RiskDecision:
    return RiskDecision(
        approved=True,
        reasons=(),
        order_notional=Decimal("1000"),
        projected_symbol_notional=Decimal("1000"),
        projected_gross_notional=Decimal("1000"),
    )


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
    prepared = lifecycle.prepare(intent(), decision(), occurred_at=NOW)
    assert prepared.record.state is OrderState.OUTBOXED
    assert len(store.pending_outbox()) == 1

    repeated = lifecycle.prepare(intent(), decision(), occurred_at=NOW)
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


def test_postgres_row_lock_and_event_key_make_duplicate_fill_at_most_once(
    store: PostgresOmsStore,
) -> None:
    lifecycle = PaperOrderLifecycle(store)
    lifecycle.prepare(intent(), decision(), occurred_at=NOW)
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


def test_postgres_event_journal_is_append_only(store: PostgresOmsStore) -> None:
    PaperOrderLifecycle(store).prepare(intent(), decision(), occurred_at=NOW)
    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_oms_events SET event_type='TAMPERED' WHERE event_id=%s",
                ("create:pg-intent-1",),
            )
        connection.rollback()
    assert store.events("pg-intent-1")[0]["event_type"] == "CREATED"


def test_bybit_entry_claim_is_durable_at_most_once_and_uses_canonical_tables(
    store: PostgresOmsStore,
) -> None:
    order_link_id = "ASTRA-DEMO-E-ABCDEF0123456789"
    intent_id = bybit_entry_intent_id(order_link_id)
    bybit_intent = OrderIntent(
        intent_id=intent_id,
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("0.01"),
        limit_price=Decimal("60001"),
        created_at=NOW,
        strategy_id="bybit-crypto-perp-v2",
    )
    bridge = PostgresBybitEntryOms(DSN)

    first = bridge.claim_entry_submission(
        bybit_intent,
        client_order_id=order_link_id,
        occurred_at=NOW,
    )
    assert first.mutation_allowed is True
    assert first.claimed_now is True
    assert first.record.state is OrderState.SUBMIT_STARTED
    assert bridge.pending_outbox() == ()

    reopened = PostgresBybitEntryOms(DSN)
    repeated = reopened.claim_entry_submission(
        bybit_intent,
        client_order_id=order_link_id,
        occurred_at=NOW,
    )
    assert repeated.mutation_allowed is False
    assert repeated.claimed_now is False
    assert repeated.record.state is OrderState.SUBMIT_STARTED

    with psycopg.connect(DSN, row_factory=psycopg.rows.dict_row) as connection:
        row = connection.execute(
            "SELECT topic, published_at, payload FROM astra_oms_outbox WHERE intent_id=%s",
            (intent_id,),
        ).fetchone()
    assert row is not None
    assert row["topic"] == "bybit_order_submit"
    assert row["published_at"] is not None
    assert row["payload"]["broker_order_type"] == "MARKET"
    assert row["payload"]["price_role"] == "PRE_ENTRY_EXECUTABLE_QUOTE_REFERENCE"

    uncertain = reopened.mark_uncertain(
        intent_id,
        occurred_at=NOW,
        reason="LOST_ACK_AND_NO_BROKER_TRUTH",
    )
    assert uncertain.state is OrderState.UNCERTAIN
    assert reopened.count_unresolved_entry_submissions() == 1
    assert reopened.count_uncertain_entries() == 1
    events = reopened.events(intent_id)
    event_types = [event["event_type"] for event in events]
    assert len(events) == 5
    assert len(set(event["event_id"] for event in events)) == 5
    assert set(event_types) == {
        "CREATED",
        "RISK_APPROVED",
        "OUTBOXED",
        "SUBMIT_STARTED",
        "UNCERTAIN",
    }
