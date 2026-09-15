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
        "PostgreSQL continuity tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.marketdata.continuity import (
    OperationalContinuityCheckpoint,
    continuity_checkpoint_id,
)
from app.marketdata.continuity_postgres import (
    PostgresOperationalContinuityStore,
    PostgresOperationalRepairBarStore,
)
from app.marketdata.operational import OperationalBar
from app.marketdata.operational_postgres import PostgresOperationalMarketDataStore

BASE = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
OBSERVED = BASE + timedelta(minutes=15, seconds=2)


def bar(index: int) -> OperationalBar:
    open_time = BASE + timedelta(minutes=5 * index)
    close_time = open_time + timedelta(minutes=5)
    price = Decimal(str(100 + index))
    start_ms = int(open_time.timestamp() * 1000)
    return OperationalBar(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        open_time=open_time,
        close_time=close_time,
        source_timestamp=close_time - timedelta(milliseconds=1),
        received_at=OBSERVED,
        source_event_id=f"kline.5.BTCUSDT:{start_ms}:{start_ms + 300_000 - 1}",
        is_final=True,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("10"),
        revision=0,
    )


@pytest.fixture()
def stores():
    marketdata = PostgresOperationalMarketDataStore(DSN)
    marketdata.migrate()
    continuity = PostgresOperationalContinuityStore(DSN)
    continuity.migrate()
    repair = PostgresOperationalRepairBarStore(DSN)
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """TRUNCATE
                astra_operational_market_continuity,
                astra_operational_decision_completions,
                astra_operational_decision_tickets,
                astra_operational_market_bar_conflicts,
                astra_operational_market_bars
            RESTART IDENTITY CASCADE"""
        )
    return marketdata, continuity, repair


def checkpoint(
    through: OperationalBar,
    *,
    previous: OperationalContinuityCheckpoint | None,
    established_at: datetime,
) -> OperationalContinuityCheckpoint:
    previous_id = None if previous is None else previous.checkpoint_id
    checkpoint_id = continuity_checkpoint_id(
        previous_checkpoint_id=previous_id,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id=through.bar_id,
        through_close_time=through.close_time,
        evidence_source="TEST",
    )
    return OperationalContinuityCheckpoint(
        checkpoint_id=checkpoint_id,
        previous_checkpoint_id=previous_id,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id=through.bar_id,
        through_close_time=through.close_time,
        established_at=established_at,
        evidence_source="TEST",
    )


def test_postgres_repair_persists_bar_without_decision_ticket(stores) -> None:
    marketdata, _, repair = stores
    value = bar(0)

    assert repair.record_without_decision(value, recorded_at=OBSERVED)
    assert not repair.record_without_decision(value, recorded_at=OBSERVED + timedelta(seconds=1))
    assert marketdata.pending_decisions() == ()
    restored = marketdata.recent_bars(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_close_time=value.close_time,
        limit=1,
    )
    assert restored == (value,)


def test_postgres_repair_accepts_matching_economics_after_live_envelope_wins(stores) -> None:
    marketdata, _, repair = stores
    repair_bar = bar(0)
    live_bar = OperationalBar(
        **{
            **repair_bar.__dict__,
            "source_timestamp": repair_bar.close_time - timedelta(seconds=2),
        }
    )
    assert live_bar.bar_id == repair_bar.bar_id
    assert live_bar.content_hash != repair_bar.content_hash
    ticket = marketdata.record_finalized_for_strategy(
        live_bar,
        strategy_id="bybit-demo-momentum-v1",
        recorded_at=OBSERVED,
    )

    assert not repair.record_without_decision(
        repair_bar,
        recorded_at=OBSERVED + timedelta(seconds=1),
    )
    assert marketdata.conflict_count() == 0
    assert marketdata.pending_decisions(strategy_id="bybit-demo-momentum-v1") == (ticket,)


def test_postgres_two_workers_repair_same_bar_once_without_ticket(stores) -> None:
    marketdata, _, _ = stores
    value = bar(0)

    def repair_once(_: int) -> bool:
        worker = PostgresOperationalRepairBarStore(DSN)
        return worker.record_without_decision(value, recorded_at=OBSERVED)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(repair_once, range(2)))

    assert sorted(results) == [False, True]
    assert marketdata.pending_decisions() == ()
    with psycopg.connect(DSN) as connection:
        bar_count = connection.execute(
            "SELECT COUNT(*) FROM astra_operational_market_bars"
        ).fetchone()[0]
        ticket_count = connection.execute(
            "SELECT COUNT(*) FROM astra_operational_decision_tickets"
        ).fetchone()[0]
    assert bar_count == 1
    assert ticket_count == 0


def test_postgres_continuity_chain_is_restart_safe_and_retry_idempotent(stores) -> None:
    _, continuity, repair = stores
    first_bar = bar(0)
    second_bar = bar(1)
    assert repair.record_without_decision(first_bar, recorded_at=OBSERVED)
    assert repair.record_without_decision(second_bar, recorded_at=OBSERVED)

    first = checkpoint(first_bar, previous=None, established_at=OBSERVED)
    second = checkpoint(
        second_bar,
        previous=first,
        established_at=OBSERVED + timedelta(seconds=1),
    )
    assert continuity.append(first)
    assert continuity.append(second)
    repeated = OperationalContinuityCheckpoint(
        **{
            **second.__dict__,
            "established_at": OBSERVED + timedelta(seconds=30),
        }
    )
    assert not continuity.append(repeated)

    reopened = PostgresOperationalContinuityStore(DSN)
    assert reopened.latest(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
    ) == second


def test_postgres_two_workers_append_same_checkpoint_exactly_once(stores) -> None:
    _, _, repair = stores
    value = bar(0)
    assert repair.record_without_decision(value, recorded_at=OBSERVED)
    candidate = checkpoint(value, previous=None, established_at=OBSERVED)

    def append_once(_: int) -> bool:
        worker = PostgresOperationalContinuityStore(DSN)
        return worker.append(candidate)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append_once, range(2)))

    assert sorted(results) == [False, True]
    with psycopg.connect(DSN) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM astra_operational_market_continuity"
        ).fetchone()[0]
    assert count == 1


def test_postgres_two_workers_cannot_fork_same_parent(stores) -> None:
    _, continuity, repair = stores
    first_bar = bar(0)
    second_bar = bar(1)
    third_bar = bar(2)
    for value in (first_bar, second_bar, third_bar):
        assert repair.record_without_decision(value, recorded_at=OBSERVED)

    root = checkpoint(first_bar, previous=None, established_at=OBSERVED)
    assert continuity.append(root)
    first_child = checkpoint(
        second_bar,
        previous=root,
        established_at=OBSERVED + timedelta(seconds=1),
    )
    competing_child = checkpoint(
        third_bar,
        previous=root,
        established_at=OBSERVED + timedelta(seconds=2),
    )

    def append_child(candidate: OperationalContinuityCheckpoint) -> str:
        worker = PostgresOperationalContinuityStore(DSN)
        try:
            return "INSERTED" if worker.append(candidate) else "IDEMPOTENT"
        except ValueError as exc:
            assert "chain fork" in str(exc)
            return "FORK_REJECTED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append_child, (first_child, competing_child)))

    assert sorted(results) == ["FORK_REJECTED", "INSERTED"]
    with psycopg.connect(DSN) as connection:
        child_count = connection.execute(
            """SELECT COUNT(*) FROM astra_operational_market_continuity
            WHERE previous_checkpoint_id=%s""",
            (root.checkpoint_id,),
        ).fetchone()[0]
    assert child_count == 1


def test_postgres_continuity_rejects_nonadvancing_chain(stores) -> None:
    _, continuity, repair = stores
    first_bar = bar(0)
    assert repair.record_without_decision(first_bar, recorded_at=OBSERVED)
    first = checkpoint(first_bar, previous=None, established_at=OBSERVED)
    assert continuity.append(first)

    same_through_id = continuity_checkpoint_id(
        previous_checkpoint_id=first.checkpoint_id,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id=first_bar.bar_id,
        through_close_time=first_bar.close_time,
        evidence_source="TEST",
    )
    nonadvancing = OperationalContinuityCheckpoint(
        checkpoint_id=same_through_id,
        previous_checkpoint_id=first.checkpoint_id,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id=first_bar.bar_id,
        through_close_time=first_bar.close_time,
        established_at=OBSERVED + timedelta(seconds=1),
        evidence_source="TEST",
    )
    with pytest.raises(ValueError, match="did not advance"):
        continuity.append(nonadvancing)


def test_postgres_continuity_rejects_missing_through_bar(stores) -> None:
    _, continuity, _ = stores
    missing = checkpoint(bar(0), previous=None, established_at=OBSERVED)
    with pytest.raises(ValueError, match="through bar is missing"):
        continuity.append(missing)


def test_postgres_continuity_journal_is_append_only(stores) -> None:
    _, continuity, repair = stores
    value = bar(0)
    assert repair.record_without_decision(value, recorded_at=OBSERVED)
    first = checkpoint(value, previous=None, established_at=OBSERVED)
    assert continuity.append(first)

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_operational_market_continuity "
                "SET evidence_source='TAMPERED' WHERE checkpoint_id=%s",
                (first.checkpoint_id,),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "DELETE FROM astra_operational_market_continuity WHERE checkpoint_id=%s",
                (first.checkpoint_id,),
            )
