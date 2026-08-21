# ruff: noqa: E402, I001

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "Bybit recovery candidate reader requires ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.domain.trading import OrderIntent, Side
from app.execution.bybit_postgres_runtime_state import PostgresBybitDemoRuntimeLease
from app.oms.bybit_entry import PostgresBybitEntryOms, bybit_entry_intent_id
from app.oms.bybit_entry_recovery_candidates import PostgresBybitEntryRecoveryCandidateReader
from app.oms.store import OrderState

NOW = datetime(2026, 8, 21, 17, 0, tzinfo=UTC)
ENTRY_LINK = "ASTRA-DEMO-E-CANDIDATE-01"
INTENT_ID = bybit_entry_intent_id(ENTRY_LINK)


@pytest.fixture(autouse=True)
def clean_recovery_candidate_state() -> None:
    oms = PostgresBybitEntryOms(DSN)
    oms.migrate()
    PostgresBybitDemoRuntimeLease(DSN, lease_name="candidate-migration").migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE astra_oms_outbox, astra_oms_events, astra_oms_orders "
            "RESTART IDENTITY CASCADE"
        )
        connection.execute("TRUNCATE astra_bybit_terminal_evidence")


def _create_order(oms: PostgresBybitEntryOms) -> None:
    oms.create(
        OrderIntent(
            intent_id=INTENT_ID,
            symbol="BTCUSDT",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            limit_price=Decimal("100000"),
            created_at=NOW,
            strategy_id="bybit-crypto-perp-v2",
        ),
        client_order_id=ENTRY_LINK,
        occurred_at=NOW,
    )


def _set_state(state: OrderState) -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "UPDATE astra_oms_orders SET state=%s, updated_at=%s WHERE intent_id=%s",
            (state.value, NOW, INTENT_ID),
        )


def test_reader_finds_post_submit_and_post_ack_crash_states_without_changing_unresolved_slo() -> None:
    oms = PostgresBybitEntryOms(DSN)
    _create_order(oms)
    reader = PostgresBybitEntryRecoveryCandidateReader(oms)

    for state in (
        OrderState.SUBMIT_STARTED,
        OrderState.UNCERTAIN,
        OrderState.RECONCILING,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ):
        _set_state(state)
        candidates = reader.load_candidates()
        assert len(candidates) == 1
        assert candidates[0].intent_id == INTENT_ID
        assert candidates[0].client_order_id == ENTRY_LINK
        assert candidates[0].state is state

    _set_state(OrderState.OUTBOXED)
    assert reader.load_candidates() == ()
    _set_state(OrderState.MANUAL)
    assert reader.load_candidates() == ()


def test_terminal_evidence_removes_historical_filled_entry_from_recovery_candidates() -> None:
    oms = PostgresBybitEntryOms(DSN)
    _create_order(oms)
    _set_state(OrderState.FILLED)
    reader = PostgresBybitEntryRecoveryCandidateReader(oms)
    assert len(reader.load_candidates()) == 1

    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_terminal_evidence
            (entry_order_link_id, checkpoint_revision, record_sha256, envelope_text, created_at)
            VALUES (%s, %s, %s, %s, %s)""",
            (ENTRY_LINK, "a" * 64, "b" * 64, "qualified-terminal-evidence", NOW),
        )

    assert reader.load_candidates() == ()
