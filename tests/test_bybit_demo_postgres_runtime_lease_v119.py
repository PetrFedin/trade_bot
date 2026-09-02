from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.execution.bybit_demo_postgres_runtime_lease import PostgresBybitDemoRuntimeLease

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "Bybit Demo PostgreSQL runtime-lease tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

MIGRATION = Path("migrations/v119/001_bybit_demo_durable_runtime.sql")
MIGRATION_SHA256 = "c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e"
LEASE_NAME = "CANONICAL_DEMO_TRADING_RUNTIME"


@pytest.fixture(autouse=True)
def clean_runtime_lease():
    store = PostgresBybitDemoRuntimeLease(DSN)
    store.migrate()
    store.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "DELETE FROM astra_bybit_demo_runtime_lease_v119 WHERE lease_name=%s",
            (LEASE_NAME,),
        )
    yield
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "DELETE FROM astra_bybit_demo_runtime_lease_v119 WHERE lease_name=%s",
            (LEASE_NAME,),
        )


def test_v119_migration_matches_frozen_sha256_and_is_idempotent() -> None:
    assert hashlib.sha256(MIGRATION.read_bytes()).hexdigest() == MIGRATION_SHA256

    store = PostgresBybitDemoRuntimeLease(DSN)
    store.migrate()
    store.migrate()

    with psycopg.connect(DSN) as connection:
        lease_table = connection.execute(
            "SELECT to_regclass('public.astra_bybit_demo_runtime_lease_v119')"
        ).fetchone()[0]
        future_checkpoint_table = connection.execute(
            "SELECT to_regclass('public.astra_bybit_demo_active_excursion_v119')"
        ).fetchone()[0]

    assert lease_table == "astra_bybit_demo_runtime_lease_v119"
    assert future_checkpoint_table == "astra_bybit_demo_active_excursion_v119"


def test_independent_stores_share_one_durable_writer_lease() -> None:
    first = PostgresBybitDemoRuntimeLease(DSN, clock_ms=lambda: 100)
    second = PostgresBybitDemoRuntimeLease(DSN, clock_ms=lambda: 200)

    acquired = first.acquire()
    assert acquired.created_time_ms == 100
    assert acquired.automatic_stale_takeover_allowed is False
    assert acquired.live_mainnet_order_routing_allowed is False
    assert acquired.order_writes_supported is False

    with pytest.raises(FileExistsError, match="already exists"):
        second.acquire()

    assert second.inspect() == acquired


def test_concurrent_acquire_has_exactly_one_winner() -> None:
    stores = [
        PostgresBybitDemoRuntimeLease(DSN, clock_ms=lambda: 100),
        PostgresBybitDemoRuntimeLease(DSN, clock_ms=lambda: 200),
    ]

    def acquire(index: int) -> str:
        try:
            return stores[index].acquire().owner_token
        except FileExistsError:
            return "FENCED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(acquire, range(2)))

    assert outcomes.count("FENCED") == 1
    winners = [value for value in outcomes if value != "FENCED"]
    assert len(winners) == 1
    assert PostgresBybitDemoRuntimeLease(DSN).inspect().owner_token == winners[0]


def test_wrong_owner_cannot_release_and_exact_owner_can() -> None:
    store = PostgresBybitDemoRuntimeLease(DSN, clock_ms=lambda: 10)
    acquired = store.acquire()

    with pytest.raises(RuntimeError, match="ownership changed"):
        store.release(owner_token="b" * 64)

    assert store.inspect() == acquired
    store.release(owner_token=acquired.owner_token)

    with pytest.raises(FileNotFoundError, match="does not exist"):
        store.inspect()


def test_old_or_orphaned_lease_never_expires_automatically() -> None:
    old_runtime = PostgresBybitDemoRuntimeLease(DSN, clock_ms=lambda: 1)
    later_runtime = PostgresBybitDemoRuntimeLease(DSN, clock_ms=lambda: 9_999_999_999_999)

    old_lease = old_runtime.acquire()
    assert old_lease.created_time_ms == 1

    with pytest.raises(FileExistsError, match="already exists"):
        later_runtime.acquire()

    assert later_runtime.inspect() == old_lease


def test_database_constraints_reject_unsafe_lease_flags() -> None:
    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO astra_bybit_demo_runtime_lease_v119
                (lease_name, owner_token, created_time_ms, process_id,
                 automatic_stale_takeover_allowed,
                 live_mainnet_order_routing_allowed, created_at)
                VALUES (%s, %s, 1, 1, true, false, now())""",
                (LEASE_NAME, "c" * 64),
            )
        connection.rollback()

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO astra_bybit_demo_runtime_lease_v119
                (lease_name, owner_token, created_time_ms, process_id,
                 automatic_stale_takeover_allowed,
                 live_mainnet_order_routing_allowed, created_at)
                VALUES (%s, %s, 1, 1, false, true, now())""",
                (LEASE_NAME, "d" * 64),
            )
        connection.rollback()
