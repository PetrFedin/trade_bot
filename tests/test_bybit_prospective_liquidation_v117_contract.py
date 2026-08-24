from __future__ import annotations

from pathlib import Path

from app.strategy.crypto_prospective_liquidation_postgres import (
    PostgresProspectiveLiquidationContextStore,
)


def test_v117_schema_is_point_in_time_append_only_and_non_trading() -> None:
    sql = Path(
        "migrations/v117/001_bybit_prospective_liquidation_context.sql"
    ).read_text(encoding="utf-8")

    required = (
        "astra_bybit_shadow_liquidation_context_v117",
        "astra_bybit_shadow_liquidation_window_v117",
        "REFERENCES astra_bybit_shadow_seed_v112(seed_id)",
        "REFERENCES astra_bybit_liquidation_subscription_v116(subscription_id)",
        "coverage_window_start_at = signal_available_at - interval '60 minutes'",
        "evaluated_at >= signal_available_at + interval '60 seconds'",
        "liquidation_feature_used_for_source_ranking boolean NOT NULL DEFAULT false",
        "parameter_retuning_performed boolean NOT NULL DEFAULT false",
        "trade_actionable boolean NOT NULL DEFAULT false",
        "strategy_promotion_allowed boolean NOT NULL DEFAULT false",
        "bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false",
        "Bybit prospective liquidation context v117 is append-only",
    )
    for fragment in required:
        assert fragment in sql


def test_v117_known_zero_requires_typed_zero_metrics() -> None:
    sql = Path(
        "migrations/v117/001_bybit_prospective_liquidation_context.sql"
    ).read_text(encoding="utf-8")

    assert "event_count = 0" in sql
    assert "total_estimated_notional_usdt = 0" in sql
    assert "normalized_long_minus_short_imbalance = 0" in sql
    assert "known_zero = true" in sql
    assert "event_count IS NULL" in sql
    assert "known_zero = false" in sql


def test_v117_store_has_no_order_write_or_live_route_capability() -> None:
    store = PostgresProspectiveLiquidationContextStore(
        "postgresql://example.invalid/astra"
    )

    assert store.order_writes_supported is False
    assert store.live_mainnet_order_routing_allowed is False
