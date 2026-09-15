from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL operational market-data tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.marketdata.operational import OperationalBar, OperationalBarConflict
from app.marketdata.operational_postgres import PostgresOperationalMarketDataStore

BASE = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
STRATEGY = "paper-momentum-v1"


def bar(index: int, *, close: str | None = None) -> OperationalBar:
    open_time = BASE + timedelta(minutes=index)
    close_time = open_time + timedelta(minutes=1)
    price = Decimal(close if close is not None else str(100 + index))
    return OperationalBar(
        provider="ALPACA",
        venue="NASDAQ",
        symbol="AAPL",
        interval_seconds=60,
        open_time=open_time,
        close_time=close_time,
        source_timestamp=close_time,
        received_at=close_time + timedelta(seconds=1),
        source_event_id=f"pg-bar-{index}",
        is_final=True,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("1000"),
    )


def recorded(value: OperationalBar, seconds: int = 2) -> datetime:
    return value.close_time + timedelta(seconds=seconds)


@pytest.fixture()
def store() -> PostgresOperationalMarketDataStore:
    value = PostgresOperationalMarketDataStore(DSN)
    value.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """TRUNCATE
                astra_operational_decision_completions,
                astra_operational_decision_tickets,
                astra_operational_market_bar_conflicts,
                astra_operational_market_bars
            RESTART IDENTITY CASCADE"""
        )
    return value


def test_postgres_migration_is_repeatable_and_restart_restores_history(
    store: PostgresOperationalMarketDataStore,
) -> None:
    store.migrate()
    values = tuple(bar(index) for index in range(3))
    tickets = tuple(
        store.record_finalized_for_strategy(
            value,
            strategy_id=STRATEGY,
            recorded_at=recorded(value),
        )
        for value in values
    )

    reopened = PostgresOperationalMarketDataStore(DSN)
    assert reopened.pending_decisions(strategy_id=STRATEGY) == tickets
    restored = reopened.recent_bars(
        provider="ALPACA",
        venue="NASDAQ",
        symbol="AAPL",
        interval_seconds=60,
        through_close_time=values[-1].close_time,
        limit=3,
    )
    assert [value.close for value in restored] == [Decimal("100"), Decimal("101"), Decimal("102")]


def test_postgres_two_workers_create_one_bar_and_one_ticket(
    store: PostgresOperationalMarketDataStore,
) -> None:
    value = bar(0)

    def persist(_: int):
        worker = PostgresOperationalMarketDataStore(DSN)
        return worker.record_finalized_for_strategy(
            value,
            strategy_id=STRATEGY,
            recorded_at=recorded(value),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(persist, range(2)))

    assert results[0] == results[1]
    assert store.pending_decisions(strategy_id=STRATEGY) == (results[0],)
    with psycopg.connect(DSN) as connection:
        bars = connection.execute(
            "SELECT COUNT(*) FROM astra_operational_market_bars"
        ).fetchone()[0]
        tickets = connection.execute(
            "SELECT COUNT(*) FROM astra_operational_decision_tickets"
        ).fetchone()[0]
    assert bars == 1
    assert tickets == 1


def test_postgres_changed_economics_persists_conflict_and_preserves_ticket(
    store: PostgresOperationalMarketDataStore,
) -> None:
    original = bar(0)
    ticket = store.record_finalized_for_strategy(
        original,
        strategy_id=STRATEGY,
        recorded_at=recorded(original),
    )

    changed = bar(0, close="105")
    with pytest.raises(OperationalBarConflict, match="OPERATIONAL_BAR_CONFLICT"):
        store.record_finalized_for_strategy(
            changed,
            strategy_id=STRATEGY,
            recorded_at=recorded(changed, seconds=3),
        )

    assert store.conflict_count() == 1
    assert store.pending_decisions(strategy_id=STRATEGY) == (ticket,)


def test_postgres_completion_is_idempotent_and_append_only(
    store: PostgresOperationalMarketDataStore,
) -> None:
    value = bar(0)
    ticket = store.record_finalized_for_strategy(
        value,
        strategy_id=STRATEGY,
        recorded_at=recorded(value),
    )

    assert store.complete_decision(
        ticket.ticket_id,
        outcome_id="NO_INTENT",
        occurred_at=recorded(value, seconds=3),
    )
    assert not store.complete_decision(
        ticket.ticket_id,
        outcome_id="NO_INTENT",
        occurred_at=recorded(value, seconds=4),
    )
    assert store.pending_decisions(strategy_id=STRATEGY) == ()

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_operational_market_bars SET close_price=1 WHERE bar_id=%s",
                (value.bar_id,),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "DELETE FROM astra_operational_decision_completions WHERE ticket_id=%s",
                (ticket.ticket_id,),
            )


def test_postgres_same_bar_can_schedule_two_strategies(
    store: PostgresOperationalMarketDataStore,
) -> None:
    value = bar(0)
    first = store.record_finalized_for_strategy(
        value,
        strategy_id="strategy-a",
        recorded_at=recorded(value),
    )
    second = store.record_finalized_for_strategy(
        value,
        strategy_id="strategy-b",
        recorded_at=recorded(value, seconds=3),
    )

    assert first.ticket_id != second.ticket_id
    assert len(store.pending_decisions()) == 2
