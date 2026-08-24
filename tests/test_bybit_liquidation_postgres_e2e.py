from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.marketdata.bybit_liquidation_forward import parse_bybit_all_liquidation_message
from app.marketdata.bybit_liquidation_postgres import PostgresBybitLiquidationStore

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_LIQUIDATION_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not _DSN, reason="ASTRA_LIQUIDATION_TEST_DSN is not configured")


def _apply_v110() -> None:
    sql = Path("migrations/v110/001_bybit_opportunity_registry.sql").read_text(encoding="utf-8")
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(sql)


def _seed_v110(now: datetime) -> str:
    snapshot_id = "b" * 64
    symbols = tuple(f"COIN{index}USDT" for index in range(1, 11))
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_opportunity_snapshot_v110
            (snapshot_id, observed_at, observed_at_ms, host, registry_limit,
             eligible_symbol_count, source_instrument_count, source_ticker_count,
             top10_complete, top10_symbols, registry_population_complete, blockers,
             excluded_reasons, snapshot_json, research_only, trade_actionable,
             strategy_promotion_allowed, live_activation_allowed,
             bybit_live_order_routing_allowed, created_at)
            VALUES (%s, %s, %s, 'api.bybit.com', 50, 10, 10, 10, true, %s::jsonb,
                    true, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
                    true, false, false, false, false, %s)
            ON CONFLICT (snapshot_id) DO NOTHING""",
            (
                snapshot_id,
                now,
                int(now.timestamp() * 1000),
                __import__("json").dumps(list(symbols)),
                now,
            ),
        )
        for rank, symbol in enumerate(symbols, start=1):
            connection.execute(
                """INSERT INTO astra_bybit_opportunity_candidate_v110
                (snapshot_id, rank, symbol, is_top10, universe_score, listing_days,
                 turnover_24h_usdt, open_interest_value_usdt, spread_bps, funding_rate,
                 price_24h_fraction, turnover_percentile, open_interest_percentile,
                 spread_quality_percentile, history_percentile, rank_drivers, signal_side,
                 trade_actionable, strategy_promotion_allowed, live_activation_allowed,
                 bybit_live_order_routing_allowed)
                VALUES (%s, %s, %s, true, 0.9, 100, 1000000, 500000, 1, 0.0001,
                        0.01, 0.9, 0.9, 0.9, 0.9,
                        '["TURNOVER_24H","OPEN_INTEREST_VALUE","SPREAD_QUALITY",'
                        '"LISTING_HISTORY"]'::jsonb, 'UNASSIGNED', false, false, false, false)
                ON CONFLICT (snapshot_id, rank) DO NOTHING""",
                (snapshot_id, rank, symbol),
            )
    return snapshot_id


def test_forward_liquidation_postgres_roundtrip_and_immutability() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    _apply_v110()
    store = PostgresBybitLiquidationStore(_DSN)
    store.migrate()
    snapshot_id = _seed_v110(now)

    universe = store.load_latest_universe(rank_limit=10, now=now)
    assert universe.source_snapshot_id == snapshot_id
    assert len(universe.symbols) == 10

    subscription_id = store.create_subscription(
        universe,
        ws_host="stream.bybit.com",
        started_at=now,
    )
    event_time_ms = int(now.timestamp() * 1000)
    payload = {
        "topic": "allLiquidation.COIN1USDT",
        "type": "snapshot",
        "ts": event_time_ms + 25,
        "data": [
            {
                "T": event_time_ms,
                "s": "COIN1USDT",
                "S": "Buy",
                "v": "2",
                "p": "100",
            }
        ],
    }
    events = parse_bybit_all_liquidation_message(payload, expected_symbols=universe.symbols)

    assert store.persist_events(subscription_id, events, received_at=now) == 1
    assert store.persist_events(subscription_id, events, received_at=now) == 0
    store.persist_status(
        subscription_id,
        state="CONNECTED",
        connection_epoch="c" * 32,
        observed_at_ms=event_time_ms,
        created_at=now,
    )
    store.persist_status(
        subscription_id,
        state="HEARTBEAT",
        connection_epoch="c" * 32,
        observed_at_ms=event_time_ms + 20_000,
        created_at=now,
    )

    with psycopg.connect(_DSN) as connection:
        aggregate = connection.execute(
            """SELECT event_count, long_liquidation_count, short_liquidation_count,
                      long_estimated_notional_usdt, total_estimated_notional_usdt,
                      normalized_long_minus_short_imbalance
               FROM astra_bybit_liquidation_5m_v116
               WHERE symbol = 'COIN1USDT'"""
        ).fetchone()
        assert aggregate is not None
        assert aggregate[0] == 1
        assert aggregate[1] == 1
        assert aggregate[2] == 0
        assert aggregate[3] == 200
        assert aggregate[4] == 200
        assert aggregate[5] == 1

        health = connection.execute(
            """SELECT last_state, connection_count, heartbeat_count
               FROM astra_bybit_liquidation_subscription_health_v116
               WHERE subscription_id = %s""",
            (subscription_id,),
        ).fetchone()
        assert health == ("HEARTBEAT", 1, 1)

    with psycopg.connect(_DSN, autocommit=True) as connection:
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute(
                "UPDATE astra_bybit_liquidation_event_v116 SET bankruptcy_price = 99"
            )
