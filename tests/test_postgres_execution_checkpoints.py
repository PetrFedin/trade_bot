from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL execution checkpoint tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.domain.trading import Side
from app.execution.execution_checkpoints import (
    ExecutionCheckpoint,
    PostgresExecutionCheckpointStore,
    canonical_execution_checkpoint_id,
)
from app.execution.execution_facts import ExecutionFact, PostgresExecutionFactStore

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def make_checkpoint(*, suffix: str, cumulative: str = "0.5") -> ExecutionCheckpoint:
    observed = NOW + timedelta(seconds=1)
    return ExecutionCheckpoint(
        checkpoint_id=canonical_execution_checkpoint_id(
            intent_id=f"pg-checkpoint-intent-{suffix}",
            broker_order_id=f"pg-checkpoint-broker-{suffix}",
            cumulative_quantity=Decimal(cumulative),
            observed_at=observed,
        ),
        intent_id=f"pg-checkpoint-intent-{suffix}",
        broker_order_id=f"pg-checkpoint-broker-{suffix}",
        client_order_id=f"pg-checkpoint-client-{suffix}",
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal(cumulative),
        broker_status="PARTIALLY_FILLED",
        observed_avg_price=Decimal("101"),
        observed_at=observed,
    )


@pytest.fixture()
def store() -> PostgresExecutionCheckpointStore:
    value = PostgresExecutionCheckpointStore(DSN)
    value.migrate()
    return value


def test_postgres_checkpoint_is_durable_idempotent_and_resolvable(
    store: PostgresExecutionCheckpointStore,
) -> None:
    suffix = uuid4().hex
    checkpoint = make_checkpoint(suffix=suffix)

    assert store.append(checkpoint)
    assert not store.append(checkpoint)
    assert checkpoint.checkpoint_id in {
        item.checkpoint_id for item in store.unresolved()
    }

    assert store.resolve_through(
        intent_id=checkpoint.intent_id,
        cumulative_quantity=Decimal("1"),
        occurred_at=NOW + timedelta(seconds=2),
    ) == 1
    assert checkpoint.checkpoint_id not in {
        item.checkpoint_id for item in store.unresolved()
    }

    reopened = PostgresExecutionCheckpointStore(DSN)
    assert checkpoint.checkpoint_id not in {
        item.checkpoint_id for item in reopened.unresolved()
    }


def test_postgres_checkpoint_conflict_fails_closed(
    store: PostgresExecutionCheckpointStore,
) -> None:
    suffix = uuid4().hex
    checkpoint = make_checkpoint(suffix=suffix)
    assert store.append(checkpoint)

    with pytest.raises(ValueError, match="EXECUTION_CHECKPOINT_CONFLICT"):
        store.append(replace(checkpoint, broker_status="FILLED"))


def test_postgres_projected_fact_resolves_late_submit_checkpoint(
    store: PostgresExecutionCheckpointStore,
) -> None:
    facts = PostgresExecutionFactStore(DSN)
    facts.migrate()
    suffix = uuid4().hex
    checkpoint = make_checkpoint(suffix=suffix, cumulative="1")
    fact = ExecutionFact(
        execution_fact_id=f"pg-checkpoint-fact-{suffix}",
        intent_id=checkpoint.intent_id,
        broker_order_id=checkpoint.broker_order_id,
        client_order_id=checkpoint.client_order_id,
        symbol="AAPL",
        side=Side.BUY,
        order_quantity=Decimal("1"),
        cumulative_quantity=Decimal("1"),
        quantity=Decimal("1"),
        price=Decimal("102"),
        fee=Decimal("0"),
        occurred_at=NOW,
    )
    facts.append(fact, source_execution_id=f"pg-checkpoint-source-{suffix}")
    facts.mark_projected(fact.execution_fact_id, occurred_at=NOW)
    store.append(checkpoint)

    assert store.resolve_from_projected_facts(
        intent_id=checkpoint.intent_id,
        occurred_at=NOW + timedelta(seconds=2),
    ) == 1
    assert checkpoint.checkpoint_id not in {
        item.checkpoint_id for item in store.unresolved()
    }


def test_postgres_checkpoint_journal_rejects_update_delete_and_truncate(
    store: PostgresExecutionCheckpointStore,
) -> None:
    suffix = uuid4().hex
    checkpoint = make_checkpoint(suffix=suffix)
    assert store.append(checkpoint)

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_execution_checkpoints SET observed_at=observed_at "
                "WHERE checkpoint_id=%s",
                (checkpoint.checkpoint_id,),
            )
        connection.rollback()

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "DELETE FROM astra_execution_checkpoints WHERE checkpoint_id=%s",
                (checkpoint.checkpoint_id,),
            )
        connection.rollback()

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("TRUNCATE astra_execution_checkpoint_events")
        connection.rollback()
