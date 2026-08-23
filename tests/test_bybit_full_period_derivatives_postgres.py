from __future__ import annotations

from pathlib import Path

from app.marketdata.bybit_full_period_derivatives_postgres import (
    PostgresBybitFullPeriodDerivativesStore,
)


def test_full_period_derivatives_store_is_non_trading_lazy_boundary() -> None:
    store = PostgresBybitFullPeriodDerivativesStore("postgresql://example.invalid/db")
    assert store.order_writes_supported is False
    assert store.live_mainnet_order_routing_allowed is False
    assert not hasattr(store, "create_order")
    assert not hasattr(store, "amend_order")
    assert not hasattr(store, "cancel_order")


def test_v114_schema_is_append_only_and_fail_closed_for_trading() -> None:
    sql = Path("migrations/v114/001_bybit_full_period_derivatives.sql").read_text(
        encoding="utf-8"
    )
    assert "astra_bybit_derivatives_day_v114" in sql
    assert "astra_bybit_open_interest_v114" in sql
    assert "astra_bybit_account_ratio_v114" in sql
    assert "astra_bybit_funding_rate_v114" in sql
    assert "append-only" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "bybit_live_order_routing_allowed = false" in sql
    assert "trade_actionable = false" in sql
    assert "source_series IN ('OPEN_INTEREST', 'ACCOUNT_RATIO', 'FUNDING')" in sql
