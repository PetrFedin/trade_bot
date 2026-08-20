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

from app.application.bybit_operator_control import PostgresBybitOperatorControl
from app.domain.trading import OrderIntent, Side
from app.oms.bybit_entry import PostgresBybitEntryOms, bybit_entry_intent_id
from app.oms.store import OrderState

NOW = datetime(2026, 8, 19, 18, 55, tzinfo=UTC)


@pytest.fixture()
def store() -> PostgresBybitEntryOms:
    value = PostgresBybitEntryOms(DSN)
    value.migrate()
    operator = PostgresBybitOperatorControl(DSN)
    operator.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE astra_oms_outbox, astra_oms_events, astra_oms_orders RESTART IDENTITY CASCADE"
        )
        connection.execute("TRUNCATE astra_bybit_operator_actions")
        connection.execute(
            """UPDATE astra_bybit_operator_state
            SET mode='PAUSED', generation=1, updated_at=%s,
                updated_by='SYSTEM', reason='ENTRY_TEST_FAIL_CLOSED_RESET'
            WHERE singleton=TRUE""",
            (NOW,),
        )
    operator.resume(
        actor="entry-test",
        reason="qualified test entry authorization",
        occurred_at=NOW,
        action_id="entry-test-resume",
    )
    return value


def _intent(order_link_id: str) -> OrderIntent:
    return OrderIntent(
        intent_id=bybit_entry_intent_id(order_link_id),
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("0.01"),
        limit_price=Decimal("60001"),
        created_at=NOW,
        strategy_id="bybit-crypto-perp-v2",
    )


def _claim(store: PostgresBybitEntryOms, suffix: str) -> str:
    order_link_id = f"ASTRA-DEMO-E-{suffix}"
    intent_id = bybit_entry_intent_id(order_link_id)
    claim = store.claim_entry_submission(
        _intent(order_link_id),
        client_order_id=order_link_id,
        occurred_at=NOW,
    )
    assert claim.mutation_allowed is True
    assert claim.claimed_now is True
    assert claim.record.state is OrderState.SUBMIT_STARTED
    return intent_id


def test_entry_claim_records_same_transaction_operator_authority(
    store: PostgresBybitEntryOms,
) -> None:
    intent_id = _claim(store, "OPERATOR-AUTH")

    risk_event = next(
        event for event in store.events(intent_id) if event["event_type"] == "RISK_APPROVED"
    )
    assert risk_event["payload"]["operator_mode"] == "RUNNING"
    assert risk_event["payload"]["operator_generation"] == 2


def test_paused_operator_rejects_entry_before_outbox_or_submit_started(
    store: PostgresBybitEntryOms,
) -> None:
    operator = PostgresBybitOperatorControl(DSN)
    operator.pause(
        actor="operator-a",
        reason="incident entry freeze",
        occurred_at=NOW,
        action_id="entry-pause",
    )
    order_link_id = "ASTRA-DEMO-E-OPERATOR-BLOCK"
    intent_id = bybit_entry_intent_id(order_link_id)

    claim = store.claim_entry_submission(
        _intent(order_link_id),
        client_order_id=order_link_id,
        occurred_at=NOW,
    )

    assert claim.mutation_allowed is False
    assert claim.claimed_now is False
    assert claim.record.state is OrderState.REJECTED
    assert store.count_unresolved_entry_submissions() == 0
    events = store.events(intent_id)
    event_types = {event["event_type"] for event in events}
    assert "REJECTED" in event_types
    assert "OUTBOXED" not in event_types
    assert "SUBMIT_STARTED" not in event_types
    rejected = next(event for event in events if event["event_type"] == "REJECTED")
    assert rejected["payload"] == {
        "broker": "BYBIT_DEMO",
        "network_mutation_attempted": False,
        "operator_generation": 3,
        "operator_mode": "PAUSED",
        "reason": "OPERATOR_NEW_ENTRY_BLOCKED",
    }
    with psycopg.connect(DSN) as connection:
        row = connection.execute(
            "SELECT count(*) FROM astra_oms_outbox WHERE intent_id=%s",
            (intent_id,),
        ).fetchone()
    assert row is not None and row[0] == 0


def test_operator_resume_only_authorizes_a_new_entry_intent(
    store: PostgresBybitEntryOms,
) -> None:
    operator = PostgresBybitOperatorControl(DSN)
    operator.pause(
        actor="operator-a",
        reason="incident entry freeze",
        occurred_at=NOW,
        action_id="entry-pause-resume-test",
    )
    blocked_link_id = "ASTRA-DEMO-E-BLOCKED-OLD"
    blocked = store.claim_entry_submission(
        _intent(blocked_link_id),
        client_order_id=blocked_link_id,
        occurred_at=NOW,
    )
    assert blocked.record.state is OrderState.REJECTED

    operator.resume(
        actor="operator-b",
        reason="incident reconciled",
        occurred_at=NOW,
        action_id="entry-resume-after-block",
    )
    new_intent_id = _claim(store, "RESUMED-NEW")

    assert store.get(new_intent_id).state is OrderState.SUBMIT_STARTED  # type: ignore[union-attr]
    assert store.get(blocked.record.intent_id).state is OrderState.REJECTED  # type: ignore[union-attr]


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
