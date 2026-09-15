from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.operational import (
    OperationalBar,
    OperationalBarConflict,
    SQLiteOperationalMarketDataStore,
)

BASE = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
STRATEGY = "paper-momentum-v1"


def bar(index: int, *, final: bool = True, close: str | None = None) -> OperationalBar:
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
        source_event_id=f"bar-{index}",
        is_final=final,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("1000"),
    )


def recorded(value: OperationalBar, seconds: int = 2) -> datetime:
    return value.close_time + timedelta(seconds=seconds)


def test_in_progress_bar_never_persists_or_schedules(tmp_path) -> None:
    store = SQLiteOperationalMarketDataStore(tmp_path / "marketdata.sqlite")
    value = bar(0, final=False)

    with pytest.raises(ValueError, match="OPERATIONAL_BAR_NOT_FINAL"):
        store.record_finalized_for_strategy(
            value,
            strategy_id=STRATEGY,
            recorded_at=recorded(value),
        )

    assert store.pending_decisions() == ()
    assert store.conflict_count() == 0


def test_final_bar_duplicate_is_idempotent_and_restart_preserves_window(tmp_path) -> None:
    path = tmp_path / "marketdata.sqlite"
    store = SQLiteOperationalMarketDataStore(path)
    values = tuple(bar(index) for index in range(3))
    tickets = tuple(
        store.record_finalized_for_strategy(
            value,
            strategy_id=STRATEGY,
            recorded_at=recorded(value),
        )
        for value in values
    )

    duplicate = OperationalBar(
        **{
            **values[-1].__dict__,
            "received_at": values[-1].received_at + timedelta(seconds=5),
        }
    )
    repeated = store.record_finalized_for_strategy(
        duplicate,
        strategy_id=STRATEGY,
        recorded_at=recorded(duplicate, seconds=7),
    )
    assert repeated == tickets[-1]
    assert len(store.pending_decisions(strategy_id=STRATEGY)) == 3

    reopened = SQLiteOperationalMarketDataStore(path)
    restored = reopened.recent_bars(
        provider="ALPACA",
        venue="NASDAQ",
        symbol="AAPL",
        interval_seconds=60,
        through_close_time=values[-1].close_time,
        limit=3,
    )
    assert [value.close for value in restored] == [Decimal("100"), Decimal("101"), Decimal("102")]
    assert [value.strategy_bar().timestamp for value in restored] == [
        value.close_time for value in values
    ]


def test_changed_final_economics_quarantines_without_second_ticket(tmp_path) -> None:
    store = SQLiteOperationalMarketDataStore(tmp_path / "marketdata.sqlite")
    original = bar(0)
    first = store.record_finalized_for_strategy(
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
    assert store.pending_decisions(strategy_id=STRATEGY) == (first,)


def test_same_final_bar_can_schedule_independent_strategy_identity(tmp_path) -> None:
    store = SQLiteOperationalMarketDataStore(tmp_path / "marketdata.sqlite")
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


def test_decision_completion_is_append_only_and_idempotent(tmp_path) -> None:
    store = SQLiteOperationalMarketDataStore(tmp_path / "marketdata.sqlite")
    value = bar(0)
    ticket = store.record_finalized_for_strategy(
        value,
        strategy_id=STRATEGY,
        recorded_at=recorded(value),
    )

    assert store.complete_decision(
        ticket.ticket_id,
        outcome_id="intent:abc",
        occurred_at=recorded(value, seconds=3),
    )
    assert not store.complete_decision(
        ticket.ticket_id,
        outcome_id="intent:abc",
        occurred_at=recorded(value, seconds=4),
    )
    assert store.pending_decisions(strategy_id=STRATEGY) == ()

    with pytest.raises(ValueError, match="OPERATIONAL_DECISION_COMPLETION_CONFLICT"):
        store.complete_decision(
            ticket.ticket_id,
            outcome_id="intent:different",
            occurred_at=recorded(value, seconds=5),
        )


def test_restart_after_finalization_before_decision_keeps_one_pending_ticket(tmp_path) -> None:
    path = tmp_path / "marketdata.sqlite"
    value = bar(0)
    first_store = SQLiteOperationalMarketDataStore(path)
    ticket = first_store.record_finalized_for_strategy(
        value,
        strategy_id=STRATEGY,
        recorded_at=recorded(value),
    )

    restarted = SQLiteOperationalMarketDataStore(path)
    assert restarted.pending_decisions(strategy_id=STRATEGY) == (ticket,)
    repeated = restarted.record_finalized_for_strategy(
        value,
        strategy_id=STRATEGY,
        recorded_at=recorded(value, seconds=10),
    )
    assert repeated == ticket
    assert restarted.pending_decisions(strategy_id=STRATEGY) == (ticket,)


def test_operational_bar_and_ticket_tables_are_append_only(tmp_path) -> None:
    path = tmp_path / "marketdata.sqlite"
    store = SQLiteOperationalMarketDataStore(path)
    value = bar(0)
    ticket = store.record_finalized_for_strategy(
        value,
        strategy_id=STRATEGY,
        recorded_at=recorded(value),
    )

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE operational_market_bars SET close_price='1' WHERE bar_id=?",
                (value.bar_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "DELETE FROM operational_decision_tickets WHERE ticket_id=?",
                (ticket.ticket_id,),
            )
    finally:
        connection.close()
