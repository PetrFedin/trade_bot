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
        "PostgreSQL decision lease tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.marketdata.continuity_postgres import PostgresOperationalContinuityStore
from app.marketdata.decision_leases import DecisionLeasePolicy, StaleDecisionLease
from app.marketdata.decision_leases_postgres import PostgresDecisionLeaseStore
from app.marketdata.operational import OperationalBar
from app.marketdata.operational_postgres import PostgresOperationalMarketDataStore

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
STRATEGY = "paper-momentum-v1"


def bar() -> OperationalBar:
    open_time = NOW - timedelta(minutes=10)
    close_time = open_time + timedelta(minutes=5)
    return OperationalBar(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        open_time=open_time,
        close_time=close_time,
        source_timestamp=close_time - timedelta(milliseconds=1),
        received_at=close_time + timedelta(seconds=1),
        source_event_id="kline.5.BTCUSDT:pg",
        is_final=True,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
        revision=0,
    )


@pytest.fixture()
def stores():
    marketdata = PostgresOperationalMarketDataStore(DSN)
    marketdata.migrate()
    continuity = PostgresOperationalContinuityStore(DSN)
    continuity.migrate()
    leases = PostgresDecisionLeaseStore(DSN)
    leases.migrate()
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
    ticket = marketdata.record_finalized_for_strategy(
        bar(),
        strategy_id=STRATEGY,
        recorded_at=NOW - timedelta(minutes=4),
    )
    return marketdata, leases, ticket


def test_postgres_two_workers_cannot_claim_same_ticket(stores) -> None:
    _, _, ticket = stores
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
    _, leases, ticket = stores
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
    assert leases.complete(
        second,
        outcome_id="planning:current",
        occurred_at=NOW + timedelta(seconds=2, milliseconds=1),
    )


def test_postgres_lease_is_invalid_at_exact_expiry_boundary(stores) -> None:
    _, leases, _ = stores
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
    _, leases, _ = stores
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


def test_postgres_lease_event_journal_is_append_only(stores) -> None:
    _, leases, _ = stores
    receipt = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
    )
    assert receipt is not None
    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_operational_decision_lease_events "
                "SET owner_id='tampered'"
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("DELETE FROM astra_operational_decision_lease_events")
