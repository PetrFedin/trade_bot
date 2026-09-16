from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.trading import OrderIntent, Side
from app.oms.indexed import IndexedDurableOmsStore
from app.oms.risk_reservations import RiskReservationBudget
from app.oms.store import OrderState

NOW = datetime(2026, 9, 16, 16, 0, tzinfo=UTC)


def intent(intent_id: str = "reservation-f20") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="reservation-f20-strategy",
    )


def budget(*, available_cash: str = "1000") -> RiskReservationBudget:
    return RiskReservationBudget(
        available_cash=Decimal(available_cash),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        maximum_symbol_notional=Decimal("1000"),
        maximum_gross_notional=Decimal("1000"),
    )


def test_reservation_event_exact_request_replay_is_idempotent(tmp_path) -> None:
    store = IndexedDurableOmsStore(tmp_path / "reservation.sqlite")
    value = intent()
    store.create(value, client_order_id="reservation-client", occurred_at=NOW)

    first = store.approve_risk_with_reservation(
        value.intent_id,
        event_id="risk:reservation-f20",
        occurred_at=NOW,
        budget=budget(),
    )
    repeated = store.approve_risk_with_reservation(
        value.intent_id,
        event_id="risk:reservation-f20",
        occurred_at=NOW,
        budget=budget(),
    )

    assert first.state is OrderState.RISK_APPROVED
    assert repeated == first
    events = [
        event
        for event in store.events(value.intent_id)
        if event["event_id"] == "risk:reservation-f20"
    ]
    assert len(events) == 1
    assert events[0]["payload"]["reservation_request"]["available_cash"] == "1000"


def test_reservation_event_id_cannot_hide_changed_budget(tmp_path) -> None:
    store = IndexedDurableOmsStore(tmp_path / "reservation.sqlite")
    value = intent()
    store.create(value, client_order_id="reservation-client", occurred_at=NOW)
    first = store.approve_risk_with_reservation(
        value.intent_id,
        event_id="risk:reservation-f20",
        occurred_at=NOW,
        budget=budget(),
    )

    with pytest.raises(ValueError, match="OMS_EVENT_ID_CONFLICT"):
        store.approve_risk_with_reservation(
            value.intent_id,
            event_id="risk:reservation-f20",
            occurred_at=NOW,
            budget=budget(available_cash="999"),
        )

    assert store.get(value.intent_id) == first


def test_reservation_event_id_is_not_reusable_by_another_intent(tmp_path) -> None:
    store = IndexedDurableOmsStore(tmp_path / "reservation.sqlite")
    first = intent("reservation-f20-a")
    second = intent("reservation-f20-b")
    store.create(first, client_order_id="reservation-client-a", occurred_at=NOW)
    store.create(second, client_order_id="reservation-client-b", occurred_at=NOW)
    store.approve_risk_with_reservation(
        first.intent_id,
        event_id="shared-risk-event",
        occurred_at=NOW,
        budget=budget(),
    )

    with pytest.raises(ValueError, match="OMS_EVENT_ID_CONFLICT"):
        store.approve_risk_with_reservation(
            second.intent_id,
            event_id="shared-risk-event",
            occurred_at=NOW,
            budget=budget(),
        )

    persisted = store.get(second.intent_id)
    assert persisted is not None
    assert persisted.state is OrderState.CREATED
