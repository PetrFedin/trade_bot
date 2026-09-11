from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL execution fact tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.domain.trading import Side
from app.execution.execution_facts import (
    ExecutionFact,
    ExecutionProjectionState,
    PostgresExecutionFactStore,
)

NOW = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)


def make_fact(*, suffix: str, price: str = "100") -> ExecutionFact:
    return ExecutionFact(
        execution_fact_id=f"pg-fact-{suffix}",
        intent_id=f"pg-intent-{suffix}",
        broker_order_id=f"pg-broker-{suffix}",
        client_order_id=f"pg-client-{suffix}",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal(price),
        fee=Decimal("0.10"),
        occurred_at=NOW,
    )


@pytest.fixture()
def store() -> PostgresExecutionFactStore:
    value = PostgresExecutionFactStore(DSN)
    value.migrate()
    return value


def test_postgres_execution_fact_is_durable_idempotent_and_projectable(
    store: PostgresExecutionFactStore,
) -> None:
    suffix = uuid4().hex
    fact = make_fact(suffix=suffix)
    source_id = f"pg-source-{suffix}"

    assert store.append(fact, source_execution_id=source_id)
    assert not store.append(fact, source_execution_id=source_id)

    unresolved = {item.fact.execution_fact_id: item for item in store.unresolved()}
    assert unresolved[fact.execution_fact_id].state is ExecutionProjectionState.PENDING

    store.mark_quarantined(
        fact.execution_fact_id,
        reason="ACCOUNTING_NOT_CONVERGED",
        occurred_at=NOW + timedelta(seconds=1),
    )
    unresolved = {item.fact.execution_fact_id: item for item in store.unresolved()}
    assert unresolved[fact.execution_fact_id].state is ExecutionProjectionState.QUARANTINED
    assert unresolved[fact.execution_fact_id].reason == "ACCOUNTING_NOT_CONVERGED"

    store.mark_projected(fact.execution_fact_id, occurred_at=NOW + timedelta(seconds=2))
    assert fact.execution_fact_id not in {
        item.fact.execution_fact_id for item in store.unresolved()
    }

    reopened = PostgresExecutionFactStore(DSN)
    assert fact.execution_fact_id not in {
        item.fact.execution_fact_id for item in reopened.unresolved()
    }


def test_postgres_execution_fact_conflicts_fail_closed(
    store: PostgresExecutionFactStore,
) -> None:
    suffix = uuid4().hex
    fact = make_fact(suffix=suffix)
    source_id = f"pg-source-{suffix}"
    assert store.append(fact, source_execution_id=source_id)

    changed = make_fact(suffix=suffix, price="101")
    with pytest.raises(ValueError, match="EXECUTION_FACT_CONFLICT"):
        store.append(changed, source_execution_id=source_id)

    other_suffix = uuid4().hex
    other = make_fact(suffix=other_suffix)
    assert store.append(other, source_execution_id=f"pg-source-{other_suffix}")
    with pytest.raises(ValueError, match="EXECUTION_SOURCE_ID_CONFLICT"):
        store.append(other, source_execution_id=source_id)

    store.mark_projected(fact.execution_fact_id, occurred_at=NOW + timedelta(seconds=1))
    store.mark_projected(other.execution_fact_id, occurred_at=NOW + timedelta(seconds=1))


def test_postgres_execution_journal_rejects_mutation_truncate_and_state_regression(
    store: PostgresExecutionFactStore,
) -> None:
    suffix = uuid4().hex
    fact = make_fact(suffix=suffix)
    assert store.append(fact, source_execution_id=f"pg-source-{suffix}")
    store.mark_projected(fact.execution_fact_id, occurred_at=NOW + timedelta(seconds=1))

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_execution_facts SET occurred_at=occurred_at "
                "WHERE execution_fact_id=%s",
                (fact.execution_fact_id,),
            )
        connection.rollback()

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "DELETE FROM astra_execution_facts WHERE execution_fact_id=%s",
                (fact.execution_fact_id,),
            )
        connection.rollback()

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("TRUNCATE astra_execution_projection_events")
        connection.rollback()

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                """INSERT INTO astra_execution_projection_events
                (event_id, execution_fact_id, state, reason, occurred_at)
                VALUES (%s, %s, 'QUARANTINED', 'late-regression', %s)""",
                (f"pg-regression-{suffix}", fact.execution_fact_id, NOW + timedelta(seconds=2)),
            )
        connection.rollback()
