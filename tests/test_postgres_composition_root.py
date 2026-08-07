from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL composition tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.application.composition import ProductConfig, build_postgres_product
from app.domain.trading import Bar, Fill, Side
from app.oms.store import OrderState
from app.risk.pretrade import RiskLimits

NOW = datetime(2026, 8, 7, 20, 30, tzinfo=UTC)


def config() -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("10000"),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
        ),
    )


def bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


def reset_product_tables() -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """TRUNCATE astra_oms_outbox, astra_oms_events, astra_oms_orders,
            astra_risk_decisions, astra_portfolio_snapshots, astra_portfolio_events
            RESTART IDENTITY CASCADE"""
        )
        connection.execute(
            """UPDATE astra_risk_chain_state
            SET last_sequence=0, last_digest=repeat('0', 64) WHERE singleton=TRUE"""
        )


def clean_runtime():
    build_postgres_product(config=config(), dsn=DSN, migrate=True)
    reset_product_tables()
    return build_postgres_product(config=config(), dsn=DSN)


def test_postgres_composition_uses_shared_durable_backends() -> None:
    runtime = clean_runtime()

    _, intent, decision = runtime.paper_pipeline.plan(bars())
    assert intent is not None and decision is not None and decision.approved
    assert runtime.paper_pipeline.last_recorded_risk is not None
    assert len(runtime.risk_admission.journal.verify()) == 1

    prepared = runtime.order_lifecycle.prepare(intent, decision, occurred_at=NOW)
    assert prepared.record.state is OrderState.OUTBOXED
    assert len(runtime.oms_store.pending_outbox()) == 1
    assert runtime.oms_store.get_by_client_order_id(prepared.client_order_id) == prepared.record
    runtime.oms_store.transition(
        intent.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="pg-submit-start",
        occurred_at=NOW,
    )
    acknowledged = runtime.oms_store.transition(
        intent.intent_id,
        OrderState.ACKNOWLEDGED,
        event_id="pg-ack",
        occurred_at=NOW,
        broker_order_id="pg-broker-order-1",
    )
    assert runtime.oms_store.get_by_broker_order_id("pg-broker-order-1") == acknowledged

    fill = Fill(
        fill_id="pg-composition-fill",
        order_intent_id=intent.intent_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        occurred_at=NOW,
        fee=Decimal("1"),
    )
    assert runtime.portfolio_store.append_fill(fill)
    replayed = runtime.portfolio_store.replay(opening_cash=runtime.config.opening_cash)
    assert replayed.cash == Decimal("9899")
    assert replayed.position("AAPL").quantity == Decimal("1")


def test_postgres_composition_restart_reopens_oms_risk_and_portfolio_truth() -> None:
    runtime = clean_runtime()
    _, intent, decision = runtime.paper_pipeline.plan(bars())
    assert intent is not None and decision is not None
    prepared = runtime.order_lifecycle.prepare(intent, decision, occurred_at=NOW)
    fill = Fill(
        fill_id="pg-restart-fill",
        order_intent_id=intent.intent_id,
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        occurred_at=NOW,
        fee=Decimal("1"),
    )
    assert runtime.portfolio_store.append_fill(fill)

    restarted = build_postgres_product(config=config(), dsn=DSN)
    order = restarted.oms_store.get(intent.intent_id)
    assert order is not None and order.state is OrderState.OUTBOXED
    assert restarted.oms_store.get_by_client_order_id(prepared.client_order_id) == order
    records = restarted.risk_admission.journal.verify()
    assert len(records) == 1 and records[0].intent_id == intent.intent_id
    assert restarted.portfolio.cash == Decimal("9899")
    assert restarted.portfolio.position("AAPL").quantity == Decimal("1")
    assert restarted.portfolio.position("AAPL").average_cost == Decimal("101")
    assert restarted.paper_pipeline.ledger is restarted.portfolio
