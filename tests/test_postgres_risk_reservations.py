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
        "PostgreSQL risk-reservation tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.domain.trading import OrderIntent, Side
from app.oms.indexed import IndexedPostgresOmsStore
from app.oms.risk_reservations import RiskReservationBudget, RiskReservationRejected
from app.oms.store import OrderState

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def budget() -> RiskReservationBudget:
    return RiskReservationBudget(
        available_cash=Decimal("110"),
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        maximum_symbol_notional=Decimal("110"),
        maximum_gross_notional=Decimal("110"),
    )


def intent(intent_id: str) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("102"),
        created_at=NOW,
        strategy_id="postgres-reservation-race",
    )


@pytest.fixture()
def store() -> IndexedPostgresOmsStore:
    value = IndexedPostgresOmsStore(DSN)
    value.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE astra_oms_outbox, astra_oms_events, astra_oms_orders "
            "RESTART IDENTITY CASCADE"
        )
    return value


def test_postgres_concurrent_approvals_share_one_account_capacity(
    store: IndexedPostgresOmsStore,
) -> None:
    for suffix in ("a", "b"):
        value = intent(f"pg-reserve-{suffix}")
        store.create(
            value,
            client_order_id=f"pg-reserve-client-{suffix}",
            occurred_at=NOW,
        )

    def reserve(intent_id: str) -> tuple[str, str]:
        worker = IndexedPostgresOmsStore(DSN)
        try:
            record = worker.approve_risk_with_reservation(
                intent_id,
                event_id=f"risk:{intent_id}",
                occurred_at=NOW,
                budget=budget(),
            )
            return intent_id, record.state.value
        except RiskReservationRejected as exc:
            return intent_id, f"REJECTED:{','.join(exc.reasons)}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = dict(executor.map(reserve, ("pg-reserve-a", "pg-reserve-b")))

    assert list(results.values()).count(OrderState.RISK_APPROVED.value) == 1
    assert sum(value.startswith("REJECTED:") for value in results.values()) == 1
    states = {
        intent_id: store.get(intent_id).state  # type: ignore[union-attr]
        for intent_id in ("pg-reserve-a", "pg-reserve-b")
    }
    assert list(states.values()).count(OrderState.RISK_APPROVED) == 1
    assert list(states.values()).count(OrderState.CREATED) == 1


def test_postgres_reservation_rejection_does_not_create_outbox(
    store: IndexedPostgresOmsStore,
) -> None:
    first = intent("pg-first")
    second = intent("pg-second")
    store.create(first, client_order_id="pg-first-client", occurred_at=NOW)
    store.create(second, client_order_id="pg-second-client", occurred_at=NOW)
    store.approve_risk_with_reservation(
        first.intent_id,
        event_id="risk:pg-first",
        occurred_at=NOW,
        budget=budget(),
    )
    store.enqueue_submit(
        first.intent_id,
        event_id="outbox:pg-first",
        occurred_at=NOW,
    )

    with pytest.raises(RiskReservationRejected) as captured:
        store.approve_risk_with_reservation(
            second.intent_id,
            event_id="risk:pg-second",
            occurred_at=NOW,
            budget=budget(),
        )
    assert captured.value.reasons == (
        "GROSS_NOTIONAL_LIMIT_EXCEEDED",
        "INSUFFICIENT_AVAILABLE_CASH",
        "SYMBOL_NOTIONAL_LIMIT_EXCEEDED",
    )
    assert len(store.pending_outbox()) == 1
    persisted = store.get(second.intent_id)
    assert persisted is not None and persisted.state is OrderState.CREATED
