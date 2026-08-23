from __future__ import annotations

from pathlib import Path

from app.strategy.crypto_readonly_account_postgres import (
    PostgresCryptoReadOnlyAccountContextStore,
)


def test_readonly_account_context_store_is_lazy_non_trading_boundary() -> None:
    store = PostgresCryptoReadOnlyAccountContextStore(
        "postgresql://example.invalid/trade_bot"
    )
    assert store.order_writes_supported is False
    assert store.live_mainnet_order_routing_allowed is False
    assert not hasattr(store, "create_order")
    assert not hasattr(store, "amend_order")
    assert not hasattr(store, "cancel_order")


def test_v115_schema_is_append_only_and_contains_no_raw_credentials() -> None:
    sql = Path(
        "migrations/v115/001_bybit_mainnet_readonly_ranking_context.sql"
    ).read_text(encoding="utf-8")
    assert "astra_bybit_mainnet_readonly_context_v115" in sql
    assert "REFERENCES astra_bybit_live_opportunity_snapshot_v111(snapshot_id)" in sql
    assert "append-only" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "api_key_fingerprint_sha256" in sql
    assert "BYBIT_MAINNET_READONLY_AVAILABLE_CAPITAL_USD_EQUIVALENT" in sql
    assert "operator_review_required = true" in sql
    assert "trade_actionable = false" in sql
    assert "order_writes_supported = false" in sql
    assert "bybit_live_order_routing_allowed = false" in sql
    assert "api_secret" not in sql
    assert "raw_api_key" not in sql
