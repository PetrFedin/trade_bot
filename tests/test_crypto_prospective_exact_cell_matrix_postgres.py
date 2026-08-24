from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.strategy.crypto_prospective_exact_cell_matrix_postgres import (
    PostgresCryptoProspectiveExactCellReader,
)

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_EXACT_CELL_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_EXACT_CELL_TEST_DSN is not configured",
)

_V110 = "1" * 64
_EVIDENCE = "2" * 64
_V111 = "3" * 64
_SEED = "4" * 64
_CELL = "BTC-LONG-BULL-OI-UP-BAL-POS-NORMAL"


def _apply(path: str) -> None:
    sql = Path(path).read_text(encoding="utf-8")
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(sql)


def _seed(signal: datetime) -> None:
    observed = signal - timedelta(minutes=5)
    observed_ms = int(observed.timestamp() * 1000)
    decision = signal - timedelta(minutes=5)
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
        connection.execute(
            """INSERT INTO astra_bybit_live_opportunity_candidate_v111
            (snapshot_id, evidence_rank, market_rank, symbol,
             qualification_state, qualification_reasons, signal_side, decision_time,
             market_universe_score, signal_quality_score,
             current_market_regime, current_open_interest_regime,
             current_crowding_regime, current_prior_funding_regime,
             current_stress_regime, current_stress_score,
             expected_net_edge_usd, planned_notional_usdt, risk_budget_usdt,
             estimated_round_trip_cost_usdt, evidence_cell_key,
             evidence_trade_count, evidence_sample_sufficient,
             evidence_profit_factor, evidence_win_rate,
             evidence_total_net_pnl_usdt, evidence_average_net_pnl_usdt,
             evidence_average_mfe_r, evidence_average_mae_r,
             evidence_drawdown_usdt, positive_historical_evidence,
             operator_review_required, trade_actionable, strategy_promotion_allowed,
             demo_activation_allowed, live_activation_allowed,
             bybit_live_order_routing_allowed)
            VALUES (%s, 1, 1, 'BTCUSDT', 'QUALIFIED_POSITIVE_EVIDENCE',
                    '["EXACT_CELL_POSITIVE"]'::jsonb, 'LONG', %s,
                    0.95, 0.9, 'BULL', 'OI_RISING', 'BALANCED', 'FUNDING_POSITIVE',
                    'STRESS_NORMAL', 1, 2, 500, 5, 1, %s,
                    40, true, 1.4, 0.6, 20, 0.5, 1.2, -0.6, 5, true,
                    true, false, false, false, false, false)""",
            (_V111, decision, _CELL),
        )
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
                "7" * 64,
                _SEED,
                _V111,
                signal,
                observed_through,
                signal + timedelta(minutes=5),
                json.dumps(outcome),
                observed_through,
            ),
        )


def test_reader_preserves_exact_source_cell_and_final_outcome() -> None:
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

    reader = PostgresCryptoProspectiveExactCellReader(_DSN)
    dataset = reader.load_dataset(maximum_final_seeds=10)

    assert len(dataset.observations) == 1
    observation = dataset.observations[0]
    assert observation.cell_context_state == "CELL_COMPLETE"
    assert observation.source_cell is not None
    assert observation.source_cell.evidence_cell_key == _CELL
    assert observation.source_cell.market_regime == "BULL"
    assert observation.source_cell.open_interest_regime == "OI_RISING"
    assert observation.source_cell.historical_profit_factor == Decimal("1.4")
    assert observation.prospective.base.horizon_240_modeled_net_pnl_usdt == Decimal("3")
    assert reader.order_writes_supported is False
    assert reader.live_mainnet_order_routing_allowed is False
