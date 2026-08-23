from __future__ import annotations

from pathlib import Path

import pytest

from app.strategy.crypto_shadow_outcome_postgres import PostgresCryptoShadowOutcomeStore


def test_shadow_store_has_no_order_surface_and_validates_limits_before_connect() -> None:
    store = PostgresCryptoShadowOutcomeStore("postgresql://shadow.invalid/db")

    assert store.order_writes_supported is False
    assert store.live_mainnet_order_routing_allowed is False
    assert not hasattr(store, "create_order")
    assert not hasattr(store, "amend_order")
    assert not hasattr(store, "cancel_order")

    with pytest.raises(ValueError, match="source limit"):
        store.unseeded_sources(limit=0)
    with pytest.raises(ValueError, match="active seed limit"):
        store.active_seeds(limit=5001)


def test_shadow_schema_is_append_only_and_fail_closed_for_trading() -> None:
    sql = Path(
        "migrations/v112/001_bybit_prospective_shadow_outcomes.sql"
    ).read_text(encoding="utf-8")

    assert "astra_bybit_shadow_seed_v112" in sql
    assert "astra_bybit_shadow_outcome_v112" in sql
    assert "append-only" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "prospective = true" in sql
    assert "operator_review_required = true" in sql
    assert "trade_actionable = false" in sql
    assert "demo_activation_allowed = false" in sql
    assert "live_activation_allowed = false" in sql
    assert "bybit_live_order_routing_allowed = false" in sql
    assert "UNIQUE (seed_id, observed_through)" in sql
