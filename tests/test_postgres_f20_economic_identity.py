from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL F20 tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.domain.trading import OrderIntent, Side
from app.oms.postgres import PostgresOmsStore
from app.oms.store import OrderState

NOW = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="pg-f20-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("10"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="pg-f20-strategy",
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
def test_postgres_same_intent_id_with_changed_economics_conflicts(
    store: PostgresOmsStore,
    changed,
) -> None:
    original = intent()
    client_order_id = "pg-f20-client"
    first = store.create(original, client_order_id=client_order_id, occurred_at=NOW)

    with pytest.raises(ValueError, match="INTENT_ID_CONFLICT"):
        store.create(changed(original), client_order_id=client_order_id, occurred_at=NOW)

    assert store.get(original.intent_id) == first


def test_postgres_identical_intent_replay_is_idempotent(store: PostgresOmsStore) -> None:
    value = intent()
    first = store.create(value, client_order_id="pg-f20-client", occurred_at=NOW)
    second = store.create(
        value,
        client_order_id="pg-f20-client",
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert second == first


def test_postgres_event_id_binds_target_payload_and_broker_identity(
    store: PostgresOmsStore,
) -> None:
    value = intent()
    store.create(value, client_order_id="pg-f20-client", occurred_at=NOW)
    store.transition(
        value.intent_id,
        OrderState.RISK_APPROVED,
        event_id="risk",
        occurred_at=NOW,
    )
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


def test_postgres_fill_event_id_cannot_hide_changed_economics(
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


def test_postgres_concurrent_divergent_intent_replay_has_one_winner(
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
