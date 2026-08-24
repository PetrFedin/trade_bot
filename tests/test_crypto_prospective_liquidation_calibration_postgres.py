from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.strategy.crypto_prospective_liquidation_calibration_postgres import (
    PostgresCryptoProspectiveLiquidationCalibrationReader,
)

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_LIQUIDATION_CALIBRATION_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_LIQUIDATION_CALIBRATION_TEST_DSN is not configured",
)

_V110 = "1" * 64
_EVIDENCE = "2" * 64
_V111 = "3" * 64
_SEED = "4" * 64
_SUBSCRIPTION = "5" * 64
_CONTEXT = "6" * 64


def _apply(path: str) -> None:
    sql = Path(path).read_text(encoding="utf-8")
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(sql)


def _seed(signal: datetime) -> None:
    observed = signal - timedelta(minutes=5)
    observed_ms = int(observed.timestamp() * 1000)
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_opportunity_snapshot_v110
            (snapshot_id, observed_at, observed_at_ms, host, registry_limit,
             eligible_symbol_count, source_instrument_count, source_ticker_count,
             top10_complete, top10_symbols, registry_population_complete, blockers,
             excluded_reasons, snapshot_json, research_only, trade_actionable,
             strategy_promotion_allowed, live_activation_allowed,
             bybit_live_order_routing_allowed, created_at)
            VALUES (%s, %s, %s, 'api.bybit.com', 50, 10, 10, 10, true,
                    '["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT",
                      "BNBUSDT","ADAUSDT","LINKUSDT","AVAXUSDT","SUIUSDT"]'::jsonb,
                    true, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
                    true, false, false, false, false, %s)""",
            (_V110, observed, observed_ms, observed),
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
            (_EVIDENCE, observed, observed),
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
            (_V111, observed, observed_ms, _V110, _EVIDENCE, observed),
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
            (_SEED, _V111, decision, signal, observed),
        )
        outcome = {
            "final": True,
            "prospective": True,
            "trade_actionable": False,
            "bybit_live_order_routing_allowed": False,
            "seed_id": _SEED,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "source_qualification_state": "QUALIFIED_POSITIVE_EVIDENCE",
            "signal_available_at": signal.isoformat(),
            "first_touch_state": "TARGET_FIRST",
            "first_touch_modeled_net_pnl_usdt": "2",
            "mfe_r": "1.5",
            "mae_r": "-0.3",
            "horizons": [
                {
                    "horizon_minutes": 15,
                    "complete": True,
                    "directional_return_fraction": "0.01",
                    "modeled_net_pnl_usdt": "1",
                },
                {
                    "horizon_minutes": 60,
                    "complete": True,
                    "directional_return_fraction": "0.02",
                    "modeled_net_pnl_usdt": "2",
                },
                {
                    "horizon_minutes": 240,
                    "complete": True,
                    "directional_return_fraction": "0.03",
                    "modeled_net_pnl_usdt": "3",
                },
            ],
        }
        observed_through = signal + timedelta(minutes=240)
        evaluation_id = "7" * 64
        connection.execute(
            """INSERT INTO astra_bybit_shadow_outcome_v112
            (evaluation_id, seed_id, source_snapshot_id,
             source_qualification_state, symbol, side, signal_available_at,
             observed_through, first_touch_state, target_hit_at, stop_hit_at,
             first_touch_modeled_net_pnl_usdt, mfe_r, mae_r,
             completed_bar_count, horizon_15_complete, horizon_60_complete,
             horizon_240_complete, final, outcome_json, prospective,
             operator_review_required, trade_actionable, demo_activation_allowed,
             live_activation_allowed, bybit_live_order_routing_allowed, created_at)
            VALUES (%s, %s, %s, 'QUALIFIED_POSITIVE_EVIDENCE', 'BTCUSDT', 'LONG',
                    %s, %s, 'TARGET_FIRST', %s, NULL, 2, 1.5, -0.3,
                    48, true, true, true, true, %s::jsonb,
                    true, true, false, false, false, false, %s)""",
            (
                evaluation_id,
                _SEED,
                _V111,
                signal,
                observed_through,
                signal + timedelta(minutes=5),
                json.dumps(outcome),
                observed_through,
            ),
        )
        coverage_start = signal - timedelta(minutes=60)
        connection.execute(
            """INSERT INTO astra_bybit_liquidation_subscription_v116
            (subscription_id, source_opportunity_snapshot_id,
             source_snapshot_observed_at, started_at, started_at_ms, ws_host,
             rank_limit, symbol_count, symbols, top10_symbols, source_schema,
             stream_topic_schema, forward_only, historical_backfill_available,
             exchange_event_id_available, research_only, trade_actionable,
             live_mainnet_order_routing_allowed, created_at)
            VALUES (%s, %s, %s, %s, %s, 'stream.bybit.com', 10, 1,
                    '["BTCUSDT"]'::jsonb, '["BTCUSDT"]'::jsonb,
                    'BYBIT_OPPORTUNITY_REGISTRY_V110', 'allLiquidation.{symbol}',
                    true, false, false, true, false, false, %s)""",
            (
                _SUBSCRIPTION,
                _V110,
                observed,
                coverage_start - timedelta(minutes=1),
                int((coverage_start - timedelta(minutes=1)).timestamp() * 1000),
                coverage_start - timedelta(minutes=1),
            ),
        )
        context_json = {
            "schema": "BYBIT_PROSPECTIVE_LIQUIDATION_CONTEXT_V117",
            "seed_id": _SEED,
        }
        connection.execute(
            """INSERT INTO astra_bybit_shadow_liquidation_context_v117
            (context_id, seed_id, source_snapshot_id, symbol, side,
             signal_available_at, coverage_window_start_at,
             coverage_subscription_id, coverage_qualified,
             coverage_reason_codes, coverage_start_status_at,
             coverage_end_status_at, maximum_status_age_seconds, evaluated_at,
             context_json, prospective,
             liquidation_feature_used_for_source_ranking,
             parameter_retuning_performed, trade_actionable,
             strategy_promotion_allowed, demo_activation_allowed,
             live_activation_allowed, bybit_live_order_routing_allowed, created_at)
            VALUES (%s, %s, %s, 'BTCUSDT', 'LONG', %s, %s, %s, true,
                    '[]'::jsonb, %s, %s, 60, %s, %s::jsonb,
                    true, false, false, false, false, false, false, false, %s)""",
            (
                _CONTEXT,
                _SEED,
                _V111,
                signal,
                coverage_start,
                _SUBSCRIPTION,
                coverage_start,
                signal,
                signal + timedelta(minutes=2),
                json.dumps(context_json),
                signal + timedelta(minutes=2),
            ),
        )
        for minutes, signed, total in ((5, 50, 50), (15, 150, 250), (60, 150, 350)):
            long_notional = (total + signed) / 2
            short_notional = total - long_notional
            connection.execute(
                """INSERT INTO astra_bybit_shadow_liquidation_window_v117
                (context_id, window_minutes, window_start_at, window_end_at,
                 event_count, long_liquidation_count, short_liquidation_count,
                 long_estimated_notional_usdt, short_estimated_notional_usdt,
                 total_estimated_notional_usdt,
                 long_minus_short_estimated_notional_usdt,
                 normalized_long_minus_short_imbalance,
                 largest_event_estimated_notional_usdt,
                 first_event_at, last_event_at, known_zero)
                VALUES (%s, %s, %s, %s, 2, 1, 1, %s, %s, %s, %s,
                        (%s::numeric / %s::numeric), %s, %s, %s, false)""",
                (
                    _CONTEXT,
                    minutes,
                    signal - timedelta(minutes=minutes),
                    signal,
                    long_notional,
                    short_notional,
                    total,
                    signed,
                    signed,
                    total,
                    max(long_notional, short_notional),
                    signal - timedelta(minutes=minutes - 1),
                    signal - timedelta(minutes=1),
                ),
            )


def test_reader_joins_final_outcome_to_exact_v117_context() -> None:
    for migration in (
        "migrations/v110/001_bybit_opportunity_registry.sql",
        "migrations/v111/001_bybit_live_evidence_registry.sql",
        "migrations/v112/001_bybit_prospective_shadow_outcomes.sql",
        "migrations/v116/001_bybit_forward_liquidation_evidence.sql",
        "migrations/v117/001_bybit_prospective_liquidation_context.sql",
    ):
        _apply(migration)
    signal = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=5)
    _seed(signal)

    reader = PostgresCryptoProspectiveLiquidationCalibrationReader(_DSN)
    dataset = reader.load_dataset(maximum_final_seeds=10)

    assert dataset.base_dataset.raw_final_seed_count == 1
    assert len(dataset.observations) == 1
    observation = dataset.observations[0]
    assert observation.base.seed_id == _SEED
    assert observation.context_state == "COVERAGE_QUALIFIED"
    assert observation.coverage_reason_codes == ()
    assert [window.window_minutes for window in observation.windows] == [5, 15, 60]
    assert observation.window(15).absolute_pressure == "LONG_LIQUIDATIONS_DOMINANT"
    assert observation.window(15).relative_pressure("LONG") == (
        "SAME_SIDE_LIQUIDATIONS_DOMINANT"
    )
    assert reader.order_writes_supported is False
    assert reader.live_mainnet_order_routing_allowed is False
