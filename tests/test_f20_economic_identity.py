from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.oms.store import DurableOmsStore, OrderState
from app.risk.pretrade import RiskDecision, risk_intent_fingerprint

NOW = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="f20-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="f20-strategy",
    )


def approved(value: OrderIntent) -> RiskDecision:
    notional = value.quantity * value.limit_price
    return RiskDecision(
        approved=True,
        reasons=(),
        order_notional=notional,
        projected_symbol_notional=notional,
        projected_gross_notional=notional,
        intent_id=value.intent_id,
        intent_fingerprint=risk_intent_fingerprint(value),
    )


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
def test_same_intent_id_with_changed_economics_fails_closed(tmp_path, changed) -> None:
    store = DurableOmsStore(tmp_path / "oms.sqlite")
    lifecycle = PaperOrderLifecycle(store)
    original = intent()
    first = lifecycle.prepare(original, approved(original), occurred_at=NOW)

    conflicting = changed(original)
    with pytest.raises(ValueError, match="INTENT_ID_CONFLICT"):
        lifecycle.prepare(conflicting, approved(conflicting), occurred_at=NOW)

    persisted = store.get(original.intent_id)
    assert persisted == first.record
    assert len(store.pending_outbox()) == 1
    assert persisted is not None
    assert persisted.symbol == "AAPL"
    assert persisted.side is Side.BUY
    assert persisted.quantity == Decimal("1")
    assert persisted.limit_price == Decimal("100")


def test_identical_intent_replay_is_idempotent(tmp_path) -> None:
    store = DurableOmsStore(tmp_path / "oms.sqlite")
    lifecycle = PaperOrderLifecycle(store)
    value = intent()

    first = lifecycle.prepare(value, approved(value), occurred_at=NOW)
    second = lifecycle.prepare(value, approved(value), occurred_at=NOW + timedelta(seconds=1))

    assert second.record == first.record
    assert second.client_order_id == first.client_order_id
    assert len(store.pending_outbox()) == 1


def test_different_intent_cannot_alias_existing_client_order_id(tmp_path) -> None:
    store = DurableOmsStore(tmp_path / "oms.sqlite")
    original = intent()
    store.create(original, client_order_id="shared-client", occurred_at=NOW)
    alias = replace(original, intent_id="f20-other-intent")

    with pytest.raises(ValueError, match="INTENT_ID_CONFLICT"):
        store.create(alias, client_order_id="shared-client", occurred_at=NOW)

    assert store.get(original.intent_id) is not None
    assert store.get(alias.intent_id) is None


def test_same_event_id_binds_target_payload_and_declared_broker_identity(tmp_path) -> None:
    store = DurableOmsStore(tmp_path / "oms.sqlite")
    lifecycle = PaperOrderLifecycle(store)
    value = intent()
    lifecycle.prepare(value, approved(value), occurred_at=NOW)
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

    with pytest.raises(ValueError, match="OMS_EVENT_ID_CONFLICT"):
        store.transition(
            value.intent_id,
            OrderState.UNCERTAIN,
            event_id="shared-event",
            occurred_at=NOW + timedelta(seconds=2),
            broker_order_id="broker-A",
            payload={"source": "submit"},
        )
    with pytest.raises(ValueError, match="OMS_EVENT_ID_CONFLICT"):
        store.transition(
            value.intent_id,
            OrderState.ACKNOWLEDGED,
            event_id="shared-event",
            occurred_at=NOW + timedelta(seconds=2),
            broker_order_id="broker-A",
            payload={"source": "reconcile"},
        )
    with pytest.raises(ValueError, match="OMS_EVENT_ID_CONFLICT"):
        store.transition(
            value.intent_id,
            OrderState.ACKNOWLEDGED,
            event_id="shared-event",
            occurred_at=NOW + timedelta(seconds=2),
            broker_order_id="broker-B",
            payload={"source": "submit"},
        )

    persisted = store.get(value.intent_id)
    assert persisted == first
    assert persisted is not None and persisted.broker_order_id == "broker-A"


def test_same_fill_event_id_cannot_hide_changed_quantity_or_broker_identity(tmp_path) -> None:
    store = DurableOmsStore(tmp_path / "oms.sqlite")
    lifecycle = PaperOrderLifecycle(store)
    value = replace(intent(), quantity=Decimal("10"))
    lifecycle.prepare(value, approved(value), occurred_at=NOW)
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

    persisted = store.get(value.intent_id)
    assert persisted == first
    assert persisted is not None and persisted.filled_quantity == Decimal("4")


def test_legacy_rows_without_identity_fingerprint_fail_closed_on_replay(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"
    store = DurableOmsStore(path)
    value = intent()
    lifecycle = PaperOrderLifecycle(store)
    lifecycle.prepare(value, approved(value), occurred_at=NOW)

    connection = store._connect()
    try:
        connection.execute(
            "UPDATE oms_orders SET intent_fingerprint=NULL WHERE intent_id=?",
            (value.intent_id,),
        )
    finally:
        connection.close()

    with pytest.raises(ValueError, match="INTENT_ID_CONFLICT"):
        lifecycle.prepare(value, approved(value), occurred_at=NOW)
