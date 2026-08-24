from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.marketdata.bybit_liquidation_postgres import (
    BybitLiquidationUniverse,
    PostgresBybitLiquidationStore,
)

_SHA = "a" * 64


def _universe() -> BybitLiquidationUniverse:
    symbols = (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "BNBUSDT",
        "ADAUSDT",
        "LINKUSDT",
        "AVAXUSDT",
        "SUIUSDT",
        "LTCUSDT",
    )
    return BybitLiquidationUniverse(
        source_snapshot_id=_SHA,
        source_snapshot_observed_at=datetime(2026, 8, 24, 6, tzinfo=UTC),
        source_host="api.bybit.eu",
        source_registry_limit=50,
        requested_rank_limit=50,
        symbols=symbols,
        top10_symbols=symbols[:10],
    )


def test_liquidation_universe_preserves_exact_ranked_top10_prefix() -> None:
    universe = _universe()

    universe.validate()

    assert universe.top10_symbols == universe.symbols[:10]
    assert len(universe.top10_symbols) == 10


def test_liquidation_universe_rejects_top10_reordering() -> None:
    universe = _universe()
    wrong_top10 = (universe.symbols[1], universe.symbols[0], *universe.symbols[2:10])

    with pytest.raises(ValueError, match="Top-10 must match"):
        BybitLiquidationUniverse(
            source_snapshot_id=universe.source_snapshot_id,
            source_snapshot_observed_at=universe.source_snapshot_observed_at,
            source_host=universe.source_host,
            source_registry_limit=universe.source_registry_limit,
            requested_rank_limit=universe.requested_rank_limit,
            symbols=universe.symbols,
            top10_symbols=wrong_top10,
        ).validate()


def test_liquidation_store_exposes_no_order_write_or_live_route() -> None:
    store = PostgresBybitLiquidationStore("postgresql://example.invalid/astra")

    assert store.order_writes_supported is False
    assert store.live_mainnet_order_routing_allowed is False


def test_v116_schema_is_append_only_forward_only_and_non_trading() -> None:
    sql = Path("migrations/v116/001_bybit_forward_liquidation_evidence.sql").read_text(
        encoding="utf-8"
    )

    required = (
        "astra_bybit_liquidation_subscription_v116",
        "astra_bybit_liquidation_event_v116",
        "astra_bybit_liquidation_stream_status_v116",
        "astra_bybit_liquidation_5m_v116",
        "astra_bybit_liquidation_subscription_health_v116",
        "forward_only boolean NOT NULL DEFAULT true CHECK (forward_only = true)",
        "historical_backfill_available boolean NOT NULL DEFAULT false",
        "exchange_event_id_available boolean NOT NULL DEFAULT false",
        "trade_actionable boolean NOT NULL DEFAULT false",
        "live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false",
        "estimated_notional_usdt = quantity_base * bankruptcy_price",
        "Bybit forward liquidation evidence v116 is append-only",
    )
    for fragment in required:
        assert fragment in sql


def test_v116_schema_links_capture_to_v110_opportunity_snapshot() -> None:
    sql = Path("migrations/v116/001_bybit_forward_liquidation_evidence.sql").read_text(
        encoding="utf-8"
    )

    assert "REFERENCES astra_bybit_opportunity_snapshot_v110(snapshot_id)" in sql
    assert "source_schema = 'BYBIT_OPPORTUNITY_REGISTRY_V110'" in sql
    assert "stream_topic_schema = 'allLiquidation.{symbol}'" in sql
