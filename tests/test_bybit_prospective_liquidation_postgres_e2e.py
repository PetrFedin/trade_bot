from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.strategy.crypto_prospective_liquidation_postgres import (
    PostgresProspectiveLiquidationContextStore,
)

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_LIQUIDATION_CONTEXT_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_LIQUIDATION_CONTEXT_TEST_DSN is not configured",
)

_V110 = "1" * 64
_EVIDENCE = "2" * 64
_V111 = "3" * 64
_SEED = "4" * 64
_SUBSCRIPTION = "5" * 64
_CONNECTION = "6" * 32


def _apply(path: str) -> None:
    sql = Path(path).read_text(encoding="utf-8")
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(sql)


def _seed_sources(signal: datetime) -> None:
    observed_at = signal - timedelta(minutes=5)
    observed_ms = int(observed_at.timestamp() * 1000)
    top10 = [
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
    ]
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
                    true, false, false, false, false, %s)""",
            (_V110, observed_at, observed_ms, json.dumps(top10), observed_at),
        )
        connection.execute(
            """INSERT INTO astra_bybit_strategy_evidence_snapshot_v111
            (evidence_snapshot_id, observed_at, trade_count, cell_count,
             minimum_cell_trades, turnover_reference_usdt, report_json,
             parameter_retuning_performed, strategy_selection_allowed,
             strategy_promotion_allowed, demo_activation_allowed,
             live_activation_allowed, bybit_live_order_routing_allowed, created_at)
            VALUES (%s, %s, 100, 10, 5, 1000000, '{}'::jsonb,
                    false, false, false, false, false, false, %s)""",
            (_EVIDENCE, observed_at, observed_at),
        )
        connection.execute(
            """INSERT INTO astra_bybit_live_opportunity_snapshot_v111
            (snapshot_id, observed_at, observed_at_ms, market_snapshot_id,
             evidence_snapshot_id, equity_usdt, equity_source,
             qualified_positive_count, qualified_mixed_count, snapshot_json,
             operator_review_required, trade_actionable,
             strategy_parameters_changed, strategy_promotion_allowed,
             demo_activation_allowed, live_activation_allowed,
             bybit_live_order_routing_allowed, created_at)
            VALUES (%s, %s, %s, %s, %s, 1000, 'TEST_EQUITY', 1, 0, '{}'::jsonb,
                    true, false, false, false, false, false, false, %s)""",
            (_V111, observed_at, observed_ms, _V110, _EVIDENCE, observed_at),
        )
        decision = signal - timedelta(minutes=5)
        connection.execute(
            """INSERT INTO astra_bybit_shadow_seed_v112
            (seed_id, source_snapshot_id, source_evidence_rank, source_market_rank,
             source_qualification_state, symbol, side, decision_bar_start_at,
             signal_available_at, entry_price, stop_price, target_price,
             planned_notional_usdt, risk_budget_usdt,
             estimated_round_trip_cost_usdt, target_net_profit_usd,
             signal_quality_score, seed_json, prospective,
             operator_review_required, trade_actionable, demo_activation_allowed,
             live_activation_allowed, bybit_live_order_routing_allowed, created_at)
            VALUES (%s, %s, 1, 1, 'QUALIFIED_POSITIVE_EVIDENCE', 'BTCUSDT', 'LONG',
                    %s, %s, 100, 99, 102, 500, 5, 1, 9, 0.9, '{}'::jsonb,
                    true, true, false, false, false, false, %s)""",
            (_SEED, _V111, decision, signal, observed_at),
        )


def _seed_liquidation_stream(signal: datetime) -> None:
    coverage_start = signal - timedelta(minutes=60)
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_liquidation_subscription_v116
            (subscription_id, source_opportunity_snapshot_id,
             source_snapshot_observed_at, started_at, started_at_ms, ws_host,
             rank_limit, symbol_count, symbols, top10_symbols, source_schema,
             stream_topic_schema, forward_only, historical_backfill_available,
             exchange_event_id_available, research_only, trade_actionable,
             live_mainnet_order_routing_allowed, created_at)
            VALUES (%s, %s, %s, %s, %s, 'stream.bybit.com', 10, 3,
                    %s::jsonb, %s::jsonb, 'BYBIT_OPPORTUNITY_REGISTRY_V110',
                    'allLiquidation.{symbol}', true, false, false, true,
                    false, false, %s)""",
            (
                _SUBSCRIPTION,
                _V110,
                signal - timedelta(minutes=5),
                coverage_start - timedelta(minutes=1),
                int((coverage_start - timedelta(minutes=1)).timestamp() * 1000),
                json.dumps(symbols),
                json.dumps(
                    [
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
                    ]
                ),
                coverage_start - timedelta(minutes=1),
            ),
        )
        status_time = coverage_start - timedelta(seconds=20)
        ordinal = 0
        while status_time <= signal:
            status_id = hashlib.sha256(
                f"{_SUBSCRIPTION}:{ordinal}:{status_time.isoformat()}".encode()
            ).hexdigest()
            connection.execute(
                """INSERT INTO astra_bybit_liquidation_stream_status_v116
                (status_id, subscription_id, connection_epoch, observed_at,
                 observed_at_ms, state, reason_code, public_data_only,
                 trade_actionable, live_mainnet_order_routing_allowed, created_at)
                VALUES (%s, %s, %s, %s, %s, 'HEARTBEAT', NULL,
                        true, false, false, %s)""",
                (
                    status_id,
                    _SUBSCRIPTION,
                    _CONNECTION,
                    status_time,
                    int(status_time.timestamp() * 1000),
                    status_time,
                ),
            )
            ordinal += 1
            status_time += timedelta(seconds=20)
        _insert_event(
            connection,
            event_id="7" * 64,
            event_time=signal - timedelta(minutes=10),
            side="LONG",
            raw_side="Buy",
            quantity="2",
            price="100",
        )
        _insert_event(
            connection,
            event_id="8" * 64,
            event_time=signal - timedelta(minutes=3),
            side="SHORT",
            raw_side="Sell",
            quantity="1",
            price="50",
        )


def _insert_event(
    connection,
    *,
    event_id: str,
    event_time: datetime,
    side: str,
    raw_side: str,
    quantity: str,
    price: str,
) -> None:
    event_ms = int(event_time.timestamp() * 1000)
    bucket_ms = (event_ms // 300_000) * 300_000
    bucket_start = datetime.fromtimestamp(bucket_ms / 1000, tz=UTC)
    connection.execute(
        """INSERT INTO astra_bybit_liquidation_event_v116
        (event_id, first_subscription_id, system_ts_ms, event_time, event_time_ms,
         bucket_start, bucket_start_ms, symbol, raw_position_side,
         liquidated_position_side, quantity_base, bankruptcy_price,
         estimated_notional_usdt, message_ordinal, dedupe_semantics,
         exchange_event_id_available, historical_backfill_available,
         trade_actionable, live_mainnet_order_routing_allowed, received_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'BTCUSDT', %s, %s,
                %s, %s, (%s::numeric * %s::numeric), 0,
                'MESSAGE_TS_EVENT_FIELDS_ORDINAL', false, false, false, false, %s)""",
        (
            event_id,
            _SUBSCRIPTION,
            event_ms + 10,
            event_time,
            event_ms,
            bucket_start,
            bucket_ms,
            raw_side,
            side,
            quantity,
            price,
            quantity,
            price,
            event_time,
        ),
    )


def test_v117_postgres_build_persist_and_append_only_contract() -> None:
    for migration in (
        "migrations/v110/001_bybit_opportunity_registry.sql",
        "migrations/v111/001_bybit_live_evidence_registry.sql",
        "migrations/v112/001_bybit_prospective_shadow_outcomes.sql",
        "migrations/v116/001_bybit_forward_liquidation_evidence.sql",
        "migrations/v117/001_bybit_prospective_liquidation_context.sql",
    ):
        _apply(migration)
    signal = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    _seed_sources(signal)
    _seed_liquidation_stream(signal)
    store = PostgresProspectiveLiquidationContextStore(_DSN)
    evaluated_at = signal + timedelta(minutes=2)

    context = store.build_for_seed(_SEED, evaluated_at=evaluated_at)

    assert context.coverage_qualified is True
    assert context.coverage_reason_codes == ()
    by_window = {item.window_minutes: item for item in context.windows}
    assert by_window[5].total_estimated_notional_usdt == 50
    assert by_window[15].total_estimated_notional_usdt == 250
    assert by_window[60].total_estimated_notional_usdt == 250
    assert store.persist(context) == context.context_id
    assert store.persist(context) == context.context_id

    with psycopg.connect(_DSN) as connection:
        header = connection.execute(
            """SELECT coverage_qualified, trade_actionable,
                      liquidation_feature_used_for_source_ranking,
                      bybit_live_order_routing_allowed
               FROM astra_bybit_shadow_liquidation_context_v117
               WHERE seed_id = %s""",
            (_SEED,),
        ).fetchone()
        assert header == (True, False, False, False)
        windows = connection.execute(
            """SELECT window_minutes, event_count, total_estimated_notional_usdt
               FROM astra_bybit_shadow_liquidation_window_v117
               WHERE context_id = %s
               ORDER BY window_minutes""",
            (context.context_id,),
        ).fetchall()
        assert windows == [(5, 1, 50), (15, 2, 250), (60, 2, 250)]

    with psycopg.connect(_DSN, autocommit=True) as connection:
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute(
                """UPDATE astra_bybit_shadow_liquidation_context_v117
                   SET coverage_qualified = false"""
            )
