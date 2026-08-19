from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL Bybit OMS recovery tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.domain.trading import OrderIntent, Side
from app.oms.bybit_entry import PostgresBybitEntryOms, bybit_entry_intent_id
from app.oms.store import OrderState

NOW = datetime(2026, 8, 19, 18, 55, tzinfo=UTC)


@pytest.fixture()
def store() -> PostgresBybitEntryOms:
    value = PostgresBybitEntryOms(DSN)
    value.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE astra_oms_outbox, astra_oms_events, astra_oms_orders RESTART IDENTITY CASCADE"
        )
    return value


def _claim(store: PostgresBybitEntryOms, suffix: str) -> str:
    order_link_id = f"ASTRA-DEMO-E-{suffix}"
    intent_id = bybit_entry_intent_id(order_link_id)
    store.claim_entry_submission(
        OrderIntent(
            intent_id=intent_id,
            symbol="BTCUSDT",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            limit_price=Decimal("60001"),
            created_at=NOW,
            strategy_id="bybit-crypto-perp-v2",
        ),
        client_order_id=order_link_id,
        occurred_at=NOW,
    )
    return intent_id


def test_found_filled_order_stays_durable_reconciliation_blocker(
    store: PostgresBybitEntryOms,
) -> None:
    intent_id = _claim(store, "RECOVERY-FILLED")
    store.mark_uncertain(intent_id, occurred_at=NOW, reason="lost ack")

    recovered = store.mark_lifecycle_reconciliation_required(
        intent_id,
        broker_order_id="broker-filled-1",
        broker_status="Filled",
        cumulative_executed_quantity=Decimal("0.01"),
        occurred_at=NOW,
    )

    assert recovered.state is OrderState.RECONCILING
    assert recovered.broker_order_id == "broker-filled-1"
    assert store.count_unresolved_entry_submissions() == 1
    assert store.unresolved_entry_submissions() == (recovered,)
    event_types = {event["event_type"] for event in store.events(intent_id)}
    assert "UNCERTAIN" in event_types
    assert "RECONCILING" in event_types


def test_rejected_zero_execution_resolves_uncertain_submission_to_terminal_rejected(
    store: PostgresBybitEntryOms,
) -> None:
    intent_id = _claim(store, "RECOVERY-REJECT")
    store.mark_uncertain(intent_id, occurred_at=NOW, reason="lost ack")

    resolved = store.resolve_rejected_without_execution(
        intent_id,
        broker_order_id="broker-rejected-1",
        cumulative_executed_quantity=Decimal("0"),
        occurred_at=NOW,
    )

    assert resolved.state is OrderState.REJECTED
    assert resolved.broker_order_id == "broker-rejected-1"
    assert store.count_unresolved_entry_submissions() == 0
    event_types = {event["event_type"] for event in store.events(intent_id)}
    assert {"RECONCILING", "RECONCILED", "REJECTED"}.issubset(event_types)
