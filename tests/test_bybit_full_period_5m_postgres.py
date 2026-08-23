from __future__ import annotations

from pathlib import Path

import pytest

from app.marketdata.bybit_full_period_5m_postgres import PostgresBybitFullPeriod5mStore


def test_full_period_store_has_no_order_surface_and_validates_before_connect() -> None:
    store = PostgresBybitFullPeriod5mStore("postgresql://history.invalid/db")

    assert store.order_writes_supported is False
    assert store.live_mainnet_order_routing_allowed is False
    assert not hasattr(store, "create_order")
    assert not hasattr(store, "amend_order")
    assert not hasattr(store, "cancel_order")

    with pytest.raises(ValueError, match="sorted and unique"):
        store.coverage_state(("ETHUSDT", "BTCUSDT"))
    with pytest.raises(ValueError, match="load interval"):
        store.load_bars(
            symbols=("BTCUSDT",),
            start_at=__import__("datetime").datetime(2026, 8, 23, tzinfo=__import__("datetime").UTC),
            end_at=__import__("datetime").datetime(2026, 8, 23, tzinfo=__import__("datetime").UTC),
        )


def test_full_period_schema_is_append_only_and_non_trading() -> None:
    sql = Path("migrations/v113/001_bybit_full_period_5m.sql").read_text(
        encoding="utf-8"
    )

    assert "astra_bybit_5m_archive_day_v113" in sql
    assert "astra_bybit_5m_bar_v113" in sql
    assert "append-only" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "UNIQUE (symbol, start_time)" in sql
    assert "trade_actionable = false" in sql
    assert "demo_activation_allowed = false" in sql
    assert "live_activation_allowed = false" in sql
    assert "bybit_live_order_routing_allowed = false" in sql
