# ruff: noqa: E402, I001

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "Bybit PostgreSQL integration tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_excursion_store import BybitDemoExcursionCheckpoint
from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionState
from app.execution.bybit_postgres_runtime_state import (
    PostgresBybitDemoExcursionStore,
    PostgresBybitDemoRuntimeLease,
)
from app.execution.bybit_startup_reconciliation import (
    BybitStartupReconciliationResult,
    BybitStartupReconciliationStatus,
)
from app.strategy.crypto_perp import CryptoSide

NOW = datetime(2026, 8, 19, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_runtime_tables() -> None:
    migrator = PostgresBybitDemoRuntimeLease(DSN, lease_name="migration-bootstrap")
    migrator.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE astra_bybit_runtime_events, astra_bybit_trades, "
            "astra_bybit_runtime_leases"
        )


def _state(
    *,
    side: CryptoSide = CryptoSide.LONG,
    current_quantity: str = "1",
) -> BybitDemoTradeExcursionState:
    return BybitDemoTradeExcursionState(
        symbol="BTCUSDT",
        side=side,
        entry_price=Decimal("60000"),
        initial_quantity=Decimal("1"),
        stop_fraction=Decimal("0.01"),
        current_quantity=Decimal(current_quantity),
    )


def _reconciled_active(
    checkpoint: BybitDemoExcursionCheckpoint,
    *,
    broker_truth_complete: bool = True,
    live_mainnet_order_routing_allowed: bool = False,
) -> BybitStartupReconciliationResult:
    side = "Buy" if checkpoint.state.side is CryptoSide.LONG else "Sell"
    current_quantity = checkpoint.state.current_quantity or checkpoint.state.initial_quantity
    position = BybitDemoPosition(
        symbol=checkpoint.state.symbol,
        side=side,
        size=current_quantity,
        average_price=checkpoint.state.entry_price,
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("30000"),
    )
    return BybitStartupReconciliationResult(
        status=BybitStartupReconciliationStatus.RESUME_MANAGEMENT,
        reasons=("BROKER_POSITION_MATCHES_ACTIVE_CHECKPOINT",),
        checkpoint=checkpoint,
        active_positions=(position,),
        open_orders=(),
        next_entry_allowed=False,
        management_allowed=True,
        terminal_recovery_required=False,
        broker_truth_complete=broker_truth_complete,
        live_mainnet_order_routing_allowed=live_mainnet_order_routing_allowed,
    )


def test_runtime_lease_is_exclusive_and_fencing_token_increments() -> None:
    first = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="exclusive-runtime",
        clock=lambda: NOW,
        process_id=101,
    )
    second = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="exclusive-runtime",
        clock=lambda: NOW,
        process_id=202,
    )

    first_lease = first.acquire()
    assert first_lease.fencing_token == 1
    assert first_lease.live_mainnet_order_routing_allowed is False

    with pytest.raises(FileExistsError, match="already active"):
        second.acquire()

    first.release(owner_token=first_lease.owner_token)
    second_lease = second.acquire()
    assert second_lease.fencing_token == 2
    second.release(owner_token=second_lease.owner_token)


@pytest.mark.parametrize("side", [CryptoSide.LONG, CryptoSide.SHORT])
def test_postgres_checkpoint_round_trip_supports_long_and_short(side: CryptoSide) -> None:
    lease_store = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name=f"roundtrip-{side.value.lower()}",
        clock=lambda: NOW,
    )
    lease = lease_store.acquire()
    store = PostgresBybitDemoExcursionStore(DSN, runtime_lease=lease_store, clock=lambda: NOW)
    order_link_id = f"ASTRA-DEMO-PG-{side.value}-1"

    initial = store.initialize(
        entry_order_link_id=order_link_id,
        state=_state(side=side),
    )
    loaded = store.load()

    assert loaded == initial
    assert loaded.state.side is side
    assert loaded.state.current_quantity == Decimal("1")

    updated = store.save(
        entry_order_link_id=order_link_id,
        state=_state(side=side, current_quantity="0.4"),
        expected_revision=initial.revision,
    )
    reloaded = store.load()

    assert reloaded == updated
    assert reloaded.revision != initial.revision
    assert reloaded.state.current_quantity == Decimal("0.4")

    store.clear(expected_revision=updated.revision)
    with pytest.raises(FileNotFoundError):
        store.load()
    lease_store.release(owner_token=lease.owner_token)

    with psycopg.connect(DSN) as connection:
        row = connection.execute(
            "SELECT lifecycle_state, closed_at FROM astra_bybit_trades "
            "WHERE entry_order_link_id=%s",
            (order_link_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "CLOSED"
    assert row[1] is not None


def test_checkpoint_mutation_requires_current_lease_fence() -> None:
    lease_store = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="required-fence",
        clock=lambda: NOW,
    )
    store = PostgresBybitDemoExcursionStore(DSN, runtime_lease=lease_store, clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="requires an acquired runtime lease"):
        store.initialize(
            entry_order_link_id="ASTRA-DEMO-PG-NO-LEASE",
            state=_state(),
        )


def test_expired_lease_cannot_be_taken_over_without_reconciled_recovery() -> None:
    now = [NOW]
    owner = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="stale-recovery",
        ttl_seconds=10,
        clock=lambda: now[0],
        process_id=101,
    )
    operator = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="stale-recovery",
        ttl_seconds=10,
        clock=lambda: now[0],
        process_id=303,
    )
    replacement = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="stale-recovery",
        ttl_seconds=10,
        clock=lambda: now[0],
        process_id=202,
    )
    first_lease = owner.acquire()
    old_store = PostgresBybitDemoExcursionStore(
        DSN,
        runtime_lease=owner,
        clock=lambda: now[0],
    )
    checkpoint = old_store.initialize(
        entry_order_link_id="ASTRA-DEMO-PG-STALE",
        state=_state(),
    )

    now[0] = NOW + timedelta(seconds=11)
    with pytest.raises(FileExistsError, match="already active"):
        replacement.acquire()

    operator.recover_expired(
        expected_fencing_token=first_lease.fencing_token,
        broker_reconciliation=_reconciled_active(checkpoint),
        operator_reason="verified old worker dead after broker-truth reconciliation",
    )
    new_lease = replacement.acquire()
    assert new_lease.fencing_token == first_lease.fencing_token + 1

    with pytest.raises(RuntimeError, match="fencing token changed"):
        old_store.save(
            entry_order_link_id=checkpoint.entry_order_link_id,
            state=_state(current_quantity="0.5"),
            expected_revision=checkpoint.revision,
        )

    new_store = PostgresBybitDemoExcursionStore(
        DSN,
        runtime_lease=replacement,
        clock=lambda: now[0],
    )
    active = new_store.load()
    new_store.clear(expected_revision=active.revision)
    replacement.release(owner_token=new_lease.owner_token)


def test_expired_recovery_requires_complete_non_mainnet_reconciliation() -> None:
    now = [NOW]
    owner = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="recovery-proof",
        ttl_seconds=10,
        clock=lambda: now[0],
    )
    operator = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="recovery-proof",
        ttl_seconds=10,
        clock=lambda: now[0],
    )
    lease = owner.acquire()
    store = PostgresBybitDemoExcursionStore(DSN, runtime_lease=owner, clock=lambda: now[0])
    checkpoint = store.initialize(
        entry_order_link_id="ASTRA-DEMO-PG-PROOF",
        state=_state(),
    )
    now[0] = NOW + timedelta(seconds=11)

    with pytest.raises(ValueError, match="complete broker truth"):
        operator.recover_expired(
            expected_fencing_token=lease.fencing_token,
            broker_reconciliation=_reconciled_active(
                checkpoint,
                broker_truth_complete=False,
            ),
            operator_reason="incomplete proof",
        )

    with pytest.raises(ValueError, match="mainnet-capable reconciliation"):
        operator.recover_expired(
            expected_fencing_token=lease.fencing_token,
            broker_reconciliation=_reconciled_active(
                checkpoint,
                live_mainnet_order_routing_allowed=True,
            ),
            operator_reason="unsafe proof",
        )


def test_runtime_event_journal_is_append_only() -> None:
    lease_store = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="append-only-runtime",
        clock=lambda: NOW,
    )
    lease = lease_store.acquire()

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_bybit_runtime_events SET event_type='TAMPERED' "
                "WHERE lease_name=%s",
                ("append-only-runtime",),
            )
        connection.rollback()

    with psycopg.connect(DSN) as connection:
        event_type = connection.execute(
            "SELECT event_type FROM astra_bybit_runtime_events WHERE lease_name=%s",
            ("append-only-runtime",),
        ).fetchone()
    assert event_type is not None
    assert event_type[0] == "LEASE_ACQUIRED"

    lease_store.release(owner_token=lease.owner_token)
