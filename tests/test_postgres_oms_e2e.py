from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import psycopg
import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.oms.postgres import PostgresOmsStore
from app.oms.store import OrderState
from app.risk.pretrade import RiskDecision

UTC = timezone.utc
NOW = datetime(2026, 8, 7, 14, 0, tzinfo=UTC)
DSN = os.environ.get(
    "ASTRA_TEST_POSTGRES_DSN",
    "postgresql://astra:astra@127.0.0.1:5432/astra",
)


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
    lifecycle = PaperOrderLifecycle(store)  # type: ignore[arg-type]
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


def test_postgres_row_lock_and_event_key_make_duplicate_fill_at_most_once(store: PostgresOmsStore) -> None:
    lifecycle = PaperOrderLifecycle(store)  # type: ignore[arg-type]
    lifecycle.prepare(intent(), decision(), occurred_at=NOW)
    store.transition("pg-intent-1", OrderState.SUBMIT_STARTED, event_id="submit", occurred_at=NOW)
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
    events = [event for event in store.events("pg-intent-1") if event["event_id"] == "shared-broker-event"]
    assert len(events) == 1
    assert store.get("pg-intent-1").version == 6  # type: ignore[union-attr]


def test_postgres_event_journal_is_append_only(store: PostgresOmsStore) -> None:
    PaperOrderLifecycle(store).prepare(intent(), decision(), occurred_at=NOW)  # type: ignore[arg-type]
    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_oms_events SET event_type='TAMPERED' WHERE event_id=%s",
                ("create:pg-intent-1",),
            )
        connection.rollback()
    assert store.events("pg-intent-1")[0]["event_type"] == "CREATED"
