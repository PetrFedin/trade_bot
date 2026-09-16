from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL decision lease tests require ASTRA_TEST_POSTGRES_DSN",
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
from app.marketdata.decision_leases import (
    DecisionLeasePolicy,
    DecisionSafetyEvidence,
    StaleDecisionLease,
    decision_safety_evidence_id,
)
from app.marketdata.decision_leases_postgres import PostgresDecisionLeaseStore
from app.marketdata.operational import OperationalBar
from app.marketdata.operational_postgres import PostgresOperationalMarketDataStore

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
BASE = NOW - timedelta(minutes=20)
STRATEGY = "paper-momentum-v1"
RACE_APPLICATION = "astra-f22d-completion-race"


def bar(index: int, *, close: str | None = None) -> OperationalBar:
    open_time = BASE + timedelta(minutes=5 * index)
    close_time = open_time + timedelta(minutes=5)
    price = Decimal(close if close is not None else str(100 + index))
    return OperationalBar(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        open_time=open_time,
        close_time=close_time,
        source_timestamp=close_time - timedelta(milliseconds=1),
        received_at=close_time + timedelta(seconds=1),
        source_event_id=f"kline.5.BTCUSDT:pg:{index}",
        is_final=True,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("10"),
        revision=0,
    )


def ready_safety(
    receipt,
    *,
    checkpoint_id: str,
    history: tuple[OperationalBar, ...],
    observed_at: datetime,
) -> DecisionSafetyEvidence:
    bar_ids = tuple(value.bar_id for value in history)
    evidence_id = decision_safety_evidence_id(
        ticket_id=receipt.ticket.ticket_id,
        owner_id=receipt.owner_id,
        release_identity=receipt.release_identity,
        fencing_token=receipt.fencing_token,
        checkpoint_id=checkpoint_id,
        first_bar_id=bar_ids[0],
        last_bar_id=bar_ids[-1],
        bar_ids=bar_ids,
        continuity_reasons=(),
        readiness_reasons=(),
        control_mode="HALTED",
        control_version=0,
        observed_at=observed_at,
    )
    return DecisionSafetyEvidence(
        evidence_id=evidence_id,
        ticket_id=receipt.ticket.ticket_id,
        owner_id=receipt.owner_id,
        release_identity=receipt.release_identity,
        fencing_token=receipt.fencing_token,
        checkpoint_id=checkpoint_id,
        first_bar_id=bar_ids[0],
        last_bar_id=bar_ids[-1],
        bar_ids=bar_ids,
        continuity_reasons=(),
        readiness_reasons=(),
        control_mode="HALTED",
        control_version=0,
        observed_at=observed_at,
    )


class CompletionRaceStore(PostgresDecisionLeaseStore):
    def _connect(self):
        connection = super()._connect()
        connection.execute(f"SET application_name = '{RACE_APPLICATION}'")
        connection.commit()
        return connection


@pytest.fixture()
def stores():
    marketdata = PostgresOperationalMarketDataStore(DSN)
    marketdata.migrate()
    continuity = PostgresOperationalContinuityStore(DSN)
    continuity.migrate()
    leases = PostgresDecisionLeaseStore(DSN)
    leases.migrate()
    repair = PostgresOperationalRepairBarStore(DSN)
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """TRUNCATE
                astra_operational_decision_safety_evidence,
                astra_operational_decision_lease_events,
                astra_operational_decision_leases,
                astra_operational_decision_completions,
                astra_operational_decision_tickets,
                astra_operational_market_continuity,
                astra_operational_market_bar_conflicts,
                astra_operational_market_bars
            RESTART IDENTITY CASCADE"""
        )
    history = (bar(0), bar(1), bar(2))
    assert repair.record_without_decision(history[0], recorded_at=NOW - timedelta(minutes=4))
    assert repair.record_without_decision(history[1], recorded_at=NOW - timedelta(minutes=4))
    ticket = marketdata.record_finalized_for_strategy(
        history[2],
        strategy_id=STRATEGY,
        recorded_at=NOW - timedelta(minutes=4),
    )
    checkpoint_id = continuity_checkpoint_id(
        previous_checkpoint_id=None,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id=history[2].bar_id,
        through_close_time=history[2].close_time,
        evidence_source="TEST",
    )
    checkpoint = OperationalContinuityCheckpoint(
        checkpoint_id=checkpoint_id,
        previous_checkpoint_id=None,
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        through_bar_id=history[2].bar_id,
        through_close_time=history[2].close_time,
        established_at=NOW - timedelta(minutes=4),
        evidence_source="TEST",
    )
    assert continuity.append(checkpoint)
    return marketdata, continuity, leases, ticket, history, checkpoint


def test_postgres_two_workers_cannot_claim_same_ticket(stores) -> None:
    _, _, _, ticket, _, _ = stores
    policy = DecisionLeasePolicy(lease_ttl=timedelta(seconds=5))

    def claim(worker: str):
        store = PostgresDecisionLeaseStore(DSN)
        return store.claim_next(
            strategy_id=STRATEGY,
            owner_id=worker,
            release_identity="release-a",
            occurred_at=NOW,
            policy=policy,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-a", "worker-b")))

    claimed = [value for value in results if value is not None]
    assert len(claimed) == 1
    assert claimed[0].ticket.ticket_id == ticket.ticket_id
    assert claimed[0].fencing_token == 1
    with psycopg.connect(DSN) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM astra_operational_decision_leases"
        ).fetchone()[0]
    assert count == 1


def test_postgres_expired_lease_reclaims_and_stale_owner_cannot_complete(stores) -> None:
    _, _, leases, ticket, _, _ = stores
    policy = DecisionLeasePolicy(lease_ttl=timedelta(seconds=2))
    first = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
        policy=policy,
    )
    assert first is not None
    second = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-b",
        release_identity="release-b",
        occurred_at=NOW + timedelta(seconds=2),
        policy=policy,
    )
    assert second is not None
    assert second.ticket.ticket_id == ticket.ticket_id
    assert second.fencing_token == 2

    with pytest.raises(StaleDecisionLease, match="stale"):
        leases.complete(
            first,
            outcome_id="planning:stale",
            occurred_at=NOW + timedelta(seconds=2, milliseconds=1),
        )
    with pytest.raises(ValueError, match="DECISION_READY_SAFETY_EVIDENCE_REQUIRED"):
        leases.complete(
            second,
            outcome_id="planning:current",
            occurred_at=NOW + timedelta(seconds=2, milliseconds=1),
        )


def test_postgres_ready_evidence_completes_idempotently(stores) -> None:
    _, _, leases, _, history, checkpoint = stores
    receipt = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
    )
    assert receipt is not None
    evidence = ready_safety(
        receipt,
        checkpoint_id=checkpoint.checkpoint_id,
        history=history,
        observed_at=NOW + timedelta(milliseconds=1),
    )
    assert leases.record_safety(evidence)
    assert leases.complete(
        receipt,
        outcome_id="planning:ready",
        occurred_at=NOW + timedelta(milliseconds=2),
    )
    assert not leases.complete(
        receipt,
        outcome_id="planning:ready",
        occurred_at=NOW + timedelta(milliseconds=3),
    )


def test_postgres_lease_is_invalid_at_exact_expiry_boundary(stores) -> None:
    _, _, leases, _, _, _ = stores
    first = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
        policy=DecisionLeasePolicy(lease_ttl=timedelta(seconds=2)),
    )
    assert first is not None
    with pytest.raises(StaleDecisionLease, match="expired"):
        leases.complete(
            first,
            outcome_id="planning:late",
            occurred_at=first.expires_at,
        )


def test_postgres_release_is_immediately_reclaimable_with_higher_fence(stores) -> None:
    _, _, leases, _, _, _ = stores
    first = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
    )
    assert first is not None
    assert leases.release(first, occurred_at=NOW + timedelta(seconds=1))
    second = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-b",
        release_identity="release-b",
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert second is not None
    assert second.fencing_token == 2


def test_postgres_lease_and_safety_journals_are_append_only(stores) -> None:
    _, _, leases, _, history, checkpoint = stores
    receipt = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
    )
    assert receipt is not None
    evidence = ready_safety(
        receipt,
        checkpoint_id=checkpoint.checkpoint_id,
        history=history,
        observed_at=NOW + timedelta(milliseconds=1),
    )
    assert leases.record_safety(evidence)
    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_operational_decision_lease_events "
                "SET owner_id='tampered'"
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("DELETE FROM astra_operational_decision_safety_evidence")


def _wait_for_completion_row_lock() -> None:
    deadline = monotonic() + 5
    with psycopg.connect(DSN, autocommit=True) as observer:
        while monotonic() < deadline:
            row = observer.execute(
                """SELECT wait_event_type
                FROM pg_stat_activity
                WHERE application_name=%s AND state='active'
                ORDER BY backend_start DESC
                LIMIT 1""",
                (RACE_APPLICATION,),
            ).fetchone()
            if row is not None and row[0] == "Lock":
                return
    raise AssertionError("decision completion never blocked on market-bar row lock")


def test_postgres_conflict_commit_linearizes_before_decision_completion(stores) -> None:
    _, _, leases, ticket, history, checkpoint = stores
    receipt = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
    )
    assert receipt is not None
    evidence = ready_safety(
        receipt,
        checkpoint_id=checkpoint.checkpoint_id,
        history=history,
        observed_at=NOW + timedelta(milliseconds=1),
    )
    assert leases.record_safety(evidence)

    middle = history[1]
    conflicting = bar(1, close="777")
    conflict_connection = psycopg.connect(DSN)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        existing_hash = conflict_connection.execute(
            """SELECT content_hash FROM astra_operational_market_bars
            WHERE bar_id=%s FOR UPDATE""",
            (middle.bar_id,),
        ).fetchone()[0]
        conflict_connection.execute(
            """INSERT INTO astra_operational_market_bar_conflicts(
                bar_id, existing_content_hash, observed_content_hash,
                observed_payload, observed_at
            ) VALUES (%s, %s, %s, %s::jsonb, %s)""",
            (
                middle.bar_id,
                existing_hash,
                conflicting.content_hash,
                json.dumps(
                    conflicting.source_payload(),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                NOW + timedelta(milliseconds=2),
            ),
        )

        race_store = CompletionRaceStore(DSN)
        future = executor.submit(
            race_store.complete,
            receipt,
            outcome_id="planning:must-not-commit",
            occurred_at=NOW + timedelta(milliseconds=3),
        )
        _wait_for_completion_row_lock()
        conflict_connection.commit()

        with pytest.raises(ValueError, match="DECISION_SAFETY_EVIDENCE_INVALIDATED"):
            future.result(timeout=5)
    finally:
        try:
            conflict_connection.rollback()
        finally:
            conflict_connection.close()
        executor.shutdown(wait=True, cancel_futures=True)

    with psycopg.connect(DSN) as connection:
        completion_count = connection.execute(
            """SELECT COUNT(*) FROM astra_operational_decision_completions
            WHERE ticket_id=%s""",
            (ticket.ticket_id,),
        ).fetchone()[0]
        conflict_count = connection.execute(
            """SELECT COUNT(*) FROM astra_operational_market_bar_conflicts
            WHERE bar_id=%s""",
            (middle.bar_id,),
        ).fetchone()[0]
    assert completion_count == 0
    assert conflict_count == 1
