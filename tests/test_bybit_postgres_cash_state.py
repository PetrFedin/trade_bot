# ruff: noqa: E402, I001

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "Bybit PostgreSQL cash tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.execution.bybit_demo_cash_reconciliation import BybitDemoCashBaseline
from app.execution.bybit_postgres_cash_state import PostgresBybitDemoCashBaselineStore

_MIGRATION = Path("migrations/product/007_bybit_cash_reconciliation.sql")


@pytest.fixture(autouse=True)
def clean_cash_table() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql)
        connection.execute("TRUNCATE astra_bybit_cash_baseline")


def _baseline() -> BybitDemoCashBaseline:
    return BybitDemoCashBaseline(
        currency="USDT",
        wallet_balance_usdt=Decimal("1000.25"),
        cumulative_all_in_pnl_usdt=Decimal("12.50"),
        session_revision="a" * 64,
        created_time_ms=1_700_000_000_000,
    )


def test_postgres_cash_baseline_roundtrips_exact_state() -> None:
    store = PostgresBybitDemoCashBaselineStore(DSN)

    persisted = store.initialize(_baseline())
    loaded = store.load()

    assert persisted == _baseline()
    assert loaded == _baseline()
    assert store.immutable_records is True
    assert store.live_mainnet_order_routing_allowed is False
    assert store.order_writes_supported is False


def test_postgres_cash_baseline_rejects_silent_reinitialization() -> None:
    store = PostgresBybitDemoCashBaselineStore(DSN)
    store.initialize(_baseline())

    with pytest.raises(FileExistsError, match="cash baseline already exists"):
        store.initialize(_baseline())


def test_postgres_cash_baseline_database_trigger_blocks_update_and_delete() -> None:
    store = PostgresBybitDemoCashBaselineStore(DSN)
    store.initialize(_baseline())

    with psycopg.connect(DSN, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="cash baseline is immutable"):
            connection.execute(
                "UPDATE astra_bybit_cash_baseline "
                "SET wallet_balance_usdt=999 WHERE baseline_key='USDT'"
            )
        with pytest.raises(psycopg.errors.RaiseException, match="cash baseline is immutable"):
            connection.execute(
                "DELETE FROM astra_bybit_cash_baseline WHERE baseline_key='USDT'"
            )

    assert store.load() == _baseline()
