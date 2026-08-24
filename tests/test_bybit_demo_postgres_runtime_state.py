from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_excursion_store import JsonFileBybitDemoExcursionStore
from app.execution.bybit_demo_excursion_tracker import (
    observe_bybit_demo_trade_excursion,
    start_bybit_demo_trade_excursion,
)
from app.execution.bybit_demo_postgres_excursion_store import (
    PostgresBybitDemoExcursionStore,
)
from app.execution.bybit_demo_postgres_runtime_lease import (
    PostgresBybitDemoRuntimeLease,
)
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_DEMO_RUNTIME_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_DEMO_RUNTIME_TEST_DSN is not configured",
)


def _migrate() -> None:
    sql = Path("migrations/v119/001_bybit_demo_durable_runtime.sql").read_text(
        encoding="utf-8"
    )
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(sql)
        connection.execute("DELETE FROM astra_bybit_demo_runtime_lease_v119")
        connection.execute("DELETE FROM astra_bybit_demo_active_excursion_v119")


def _plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-24T12:00:00+00:00",
        reference_price=Decimal("100"),
        notional_usdt=Decimal("200"),
        reference_quantity=Decimal("2"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.05"),
        estimated_round_trip_cost_usdt=Decimal("1"),
        estimated_stop_loss_after_cost_usdt=Decimal("11"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.105"),
        expected_move_fraction=Decimal("0.15"),
        expected_net_edge_usd=Decimal("29"),
        quality_score=Decimal("2"),
    )


def _position(*, size: str = "2", unrealised: str = "0") -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal(size),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal(unrealised),
        liquidation_price=Decimal("50"),
    )


def _quote(mark: str, server_time_ms: int) -> BybitDemoMarketQuote:
    mark_price = Decimal(mark)
    return BybitDemoMarketQuote(
        symbol="BTCUSDT",
        last_price=mark_price,
        mark_price=mark_price,
        bid_price=mark_price - Decimal("0.01"),
        ask_price=mark_price + Decimal("0.01"),
        server_time_ms=server_time_ms,
        received_time_ms=server_time_ms + 100,
        age_ms=100,
    )


def _initial_state():
    return start_bybit_demo_trade_excursion(_plan(), position=_position())


def _observed_state():
    return observe_bybit_demo_trade_excursion(
        _initial_state(),
        position=_position(size="1", unrealised="10"),
        quote=_quote("110", 1_000),
    )


def test_postgres_runtime_lease_is_single_writer_across_store_instances() -> None:
    _migrate()
    first_store = PostgresBybitDemoRuntimeLease(_DSN, clock_ms=lambda: 1_000)
    second_store = PostgresBybitDemoRuntimeLease(_DSN, clock_ms=lambda: 2_000)

    lease = first_store.acquire()
    inspected = second_store.inspect()

    assert inspected == lease
    assert lease.created_time_ms == 1_000
    assert first_store.live_mainnet_order_routing_allowed is False
    assert first_store.order_writes_supported is False
    assert first_store.automatic_stale_takeover_allowed is False

    with pytest.raises(FileExistsError, match="already exists"):
        second_store.acquire()

    with pytest.raises(RuntimeError, match="ownership changed"):
        second_store.release(owner_token="f" * 64)

    first_store.release(owner_token=lease.owner_token)
    with pytest.raises(FileNotFoundError):
        second_store.inspect()


def test_postgres_runtime_lease_never_auto_takes_over_orphaned_row() -> None:
    _migrate()
    first_store = PostgresBybitDemoRuntimeLease(_DSN, clock_ms=lambda: 1)
    second_store = PostgresBybitDemoRuntimeLease(_DSN, clock_ms=lambda: 9_999_999_999)
    lease = first_store.acquire()

    with pytest.raises(FileExistsError):
        second_store.acquire()
    assert second_store.inspect().owner_token == lease.owner_token

    first_store.release(owner_token=lease.owner_token)


def test_postgres_excursion_round_trip_and_revision_match_file_backend(tmp_path) -> None:
    _migrate()
    state = _observed_state()
    entry_order_link_id = "ASTRA-DEMO-E-PG-EXCURSION"
    postgres_store = PostgresBybitDemoExcursionStore(_DSN)
    file_store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")

    postgres_checkpoint = postgres_store.initialize(
        entry_order_link_id=entry_order_link_id,
        state=state,
    )
    file_checkpoint = file_store.initialize(
        entry_order_link_id=entry_order_link_id,
        state=state,
    )
    loaded = PostgresBybitDemoExcursionStore(_DSN).load()

    assert postgres_checkpoint.revision == file_checkpoint.revision
    assert loaded == postgres_checkpoint
    assert loaded.state.observation_count == 1
    assert loaded.state.observed_peak_favorable_r == Decimal("2")
    assert loaded.state.partial_close_seen is True
    assert loaded.state.current_quantity == Decimal("1")
    assert postgres_store.live_mainnet_order_routing_allowed is False
    assert postgres_store.order_writes_supported is False


def test_postgres_excursion_cas_rejects_stale_save_and_clear() -> None:
    _migrate()
    store_a = PostgresBybitDemoExcursionStore(_DSN)
    store_b = PostgresBybitDemoExcursionStore(_DSN)
    entry_order_link_id = "ASTRA-DEMO-E-PG-CAS"
    initial = store_a.initialize(
        entry_order_link_id=entry_order_link_id,
        state=_initial_state(),
    )
    current = store_b.save(
        entry_order_link_id=entry_order_link_id,
        state=_observed_state(),
        expected_revision=initial.revision,
    )

    with pytest.raises(RuntimeError, match="revision changed concurrently"):
        store_a.save(
            entry_order_link_id=entry_order_link_id,
            state=_observed_state(),
            expected_revision=initial.revision,
        )

    with pytest.raises(RuntimeError, match="revision changed before clear"):
        store_a.clear(expected_revision=initial.revision)

    store_b.clear(expected_revision=current.revision)
    with pytest.raises(FileNotFoundError):
        store_a.load()


def test_postgres_excursion_rejects_tampered_state_checksum() -> None:
    _migrate()
    store = PostgresBybitDemoExcursionStore(_DSN)
    checkpoint = store.initialize(
        entry_order_link_id="ASTRA-DEMO-E-PG-TAMPER",
        state=_observed_state(),
    )

    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(
            """UPDATE astra_bybit_demo_active_excursion_v119
               SET state_json = jsonb_set(state_json, '{observed_peak_favorable_r}', '"999"')
               WHERE revision=%s""",
            (checkpoint.revision,),
        )

    with pytest.raises(ValueError, match="checksum mismatch"):
        store.load()


def test_postgres_excursion_initialize_is_single_active_trade() -> None:
    _migrate()
    first_store = PostgresBybitDemoExcursionStore(_DSN)
    second_store = PostgresBybitDemoExcursionStore(_DSN)
    first_store.initialize(
        entry_order_link_id="ASTRA-DEMO-E-PG-FIRST",
        state=_initial_state(),
    )

    with pytest.raises(FileExistsError, match="already exists"):
        second_store.initialize(
            entry_order_link_id="ASTRA-DEMO-E-PG-SECOND",
            state=_initial_state(),
        )
