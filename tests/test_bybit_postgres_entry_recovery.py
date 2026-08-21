# ruff: noqa: E402, I001

from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "Bybit PostgreSQL entry recovery tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.execution.bybit_entry_recovery import BybitEntryRecoveryEnvelope
from app.execution.bybit_postgres_entry_recovery import PostgresBybitEntryRecoveryStore
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan

_MIGRATION = Path("migrations/product/008_bybit_entry_recovery.sql")


@pytest.fixture(autouse=True)
def clean_recovery_table() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql)
        connection.execute("TRUNCATE astra_bybit_entry_recovery")


def _envelope() -> BybitEntryRecoveryEnvelope:
    return BybitEntryRecoveryEnvelope(
        entry_order_link_id="ASTRA-DEMO-E-PG-RECOVERY-1",
        order_side="Buy",
        approved_order_quantity=Decimal("0.01"),
        trade_plan=CryptoTradePlan(
            symbol="BTCUSDT",
            side=CryptoSide.LONG,
            decision_time="2026-08-21T12:00:00+00:00",
            reference_price=Decimal("100000"),
            notional_usdt=Decimal("1000"),
            reference_quantity=Decimal("0.01"),
            risk_budget_usdt=Decimal("10"),
            stop_fraction=Decimal("0.01"),
            estimated_round_trip_cost_usdt=Decimal("1.10"),
            estimated_stop_loss_after_cost_usdt=Decimal("11.10"),
            target_net_profit_usd=Decimal("20"),
            required_move_fraction=Decimal("0.0211"),
            expected_move_fraction=Decimal("0.05"),
            expected_net_edge_usd=Decimal("38.90"),
            quality_score=Decimal("0.95"),
        ),
        instrument=BybitInstrumentSpec(
            symbol="BTCUSDT",
            status="Trading",
            contract_type="LinearPerpetual",
            base_coin="BTC",
            quote_coin="USDT",
            settle_coin="USDT",
            tick_size=Decimal("0.10"),
            min_order_qty=Decimal("0.001"),
            qty_step=Decimal("0.001"),
            min_notional_value=Decimal("5"),
            max_market_order_qty=Decimal("100"),
            max_leverage=Decimal("100"),
            funding_interval_minutes=480,
        ),
        strategy_config=CryptoPerpStrategyConfig(taker_fee_rate=Decimal("0.00055")),
        planned_exit_mode="FIXED_20_TARGET",
    )


def test_postgres_entry_recovery_roundtrips_and_exact_retry_is_idempotent() -> None:
    store = PostgresBybitEntryRecoveryStore(DSN)
    envelope = _envelope()

    first = store.persist(envelope)
    second = store.persist(envelope)
    loaded = store.load(entry_order_link_id=envelope.entry_order_link_id)

    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert first.record_sha256 == second.record_sha256 == loaded.record_sha256
    assert loaded.envelope == envelope
    assert store.immutable_records is True
    assert store.live_mainnet_order_routing_allowed is False
    assert store.order_writes_supported is False


def test_postgres_entry_recovery_rejects_conflicting_same_order_link_id() -> None:
    store = PostgresBybitEntryRecoveryStore(DSN)
    envelope = _envelope()
    store.persist(envelope)
    conflicting = replace(
        envelope,
        trade_plan=replace(
            envelope.trade_plan,
            expected_net_edge_usd=Decimal("37.00"),
        ),
    )

    with pytest.raises(RuntimeError, match="conflict"):
        store.persist(conflicting)

    assert store.load(entry_order_link_id=envelope.entry_order_link_id).envelope == envelope


def test_postgres_entry_recovery_database_trigger_blocks_update_and_delete() -> None:
    store = PostgresBybitEntryRecoveryStore(DSN)
    envelope = _envelope()
    store.persist(envelope)

    with psycopg.connect(DSN, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="recovery envelope is immutable"):
            connection.execute(
                "UPDATE astra_bybit_entry_recovery "
                "SET record_sha256=%s WHERE entry_order_link_id=%s",
                ("0" * 64, envelope.entry_order_link_id),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="recovery envelope is immutable"):
            connection.execute(
                "DELETE FROM astra_bybit_entry_recovery WHERE entry_order_link_id=%s",
                (envelope.entry_order_link_id,),
            )

    assert store.load(entry_order_link_id=envelope.entry_order_link_id).envelope == envelope
