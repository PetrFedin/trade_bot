BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_strategy_evidence_snapshot_v111 (
    evidence_snapshot_id text PRIMARY KEY CHECK (evidence_snapshot_id ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    trade_count integer NOT NULL CHECK (trade_count >= 0),
    cell_count integer NOT NULL CHECK (cell_count >= 0),
    minimum_cell_trades integer NOT NULL CHECK (minimum_cell_trades > 0),
    turnover_reference_usdt numeric NOT NULL CHECK (turnover_reference_usdt >= 0),
    report_json jsonb NOT NULL,
    parameter_retuning_performed boolean NOT NULL DEFAULT false
        CHECK (parameter_retuning_performed = false),
    strategy_selection_allowed boolean NOT NULL DEFAULT false
        CHECK (strategy_selection_allowed = false),
    strategy_promotion_allowed boolean NOT NULL DEFAULT false
        CHECK (strategy_promotion_allowed = false),
    demo_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (demo_activation_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_bybit_live_opportunity_snapshot_v111 (
    snapshot_id text PRIMARY KEY CHECK (snapshot_id ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    observed_at_ms bigint NOT NULL CHECK (observed_at_ms >= 0),
    market_snapshot_id text NOT NULL
        REFERENCES astra_bybit_opportunity_snapshot_v110(snapshot_id) ON DELETE RESTRICT,
    evidence_snapshot_id text NOT NULL
        REFERENCES astra_bybit_strategy_evidence_snapshot_v111(evidence_snapshot_id)
        ON DELETE RESTRICT,
    equity_usdt numeric NOT NULL CHECK (equity_usdt > 0),
    equity_source text NOT NULL CHECK (equity_source <> ''),
    qualified_positive_count integer NOT NULL CHECK (qualified_positive_count >= 0),
    qualified_mixed_count integer NOT NULL CHECK (qualified_mixed_count >= 0),
    snapshot_json jsonb NOT NULL,
    operator_review_required boolean NOT NULL DEFAULT true
        CHECK (operator_review_required = true),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    strategy_parameters_changed boolean NOT NULL DEFAULT false
        CHECK (strategy_parameters_changed = false),
    strategy_promotion_allowed boolean NOT NULL DEFAULT false
        CHECK (strategy_promotion_allowed = false),
    demo_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (demo_activation_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    UNIQUE (market_snapshot_id, evidence_snapshot_id, equity_source, equity_usdt)
);

CREATE TABLE IF NOT EXISTS astra_bybit_live_opportunity_candidate_v111 (
    snapshot_id text NOT NULL
        REFERENCES astra_bybit_live_opportunity_snapshot_v111(snapshot_id) ON DELETE RESTRICT,
    evidence_rank integer NOT NULL CHECK (evidence_rank BETWEEN 1 AND 50),
    market_rank integer NOT NULL CHECK (market_rank BETWEEN 1 AND 50),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    qualification_state text NOT NULL CHECK (
        qualification_state IN (
            'QUALIFIED_POSITIVE_EVIDENCE',
            'QUALIFIED_MIXED_EVIDENCE',
            'NO_SAMPLE_SUFFICIENT_EXACT_CELL',
            'DERIVATIVES_CONTEXT_INCOMPLETE',
            'TRADE_PLAN_REJECTED',
            'NO_FIXED_STRATEGY_SIGNAL',
            'MARKET_HISTORY_UNAVAILABLE'
        )
    ),
    qualification_reasons jsonb NOT NULL,
    signal_side text NULL CHECK (signal_side IS NULL OR signal_side IN ('LONG', 'SHORT')),
    decision_time timestamptz NULL,
    market_universe_score numeric NOT NULL CHECK (
        market_universe_score >= 0 AND market_universe_score <= 1
    ),
    signal_quality_score numeric NULL,
    current_market_regime text NULL,
    current_open_interest_regime text NULL,
    current_crowding_regime text NULL,
    current_prior_funding_regime text NULL,
    current_stress_regime text NULL,
    current_stress_score integer NULL CHECK (
        current_stress_score IS NULL OR current_stress_score BETWEEN 0 AND 5
    ),
    expected_net_edge_usd numeric NULL,
    planned_notional_usdt numeric NULL,
    risk_budget_usdt numeric NULL,
    estimated_round_trip_cost_usdt numeric NULL,
    evidence_cell_key text NULL,
    evidence_trade_count integer NULL CHECK (
        evidence_trade_count IS NULL OR evidence_trade_count >= 0
    ),
    evidence_sample_sufficient boolean NOT NULL,
    evidence_profit_factor numeric NULL,
    evidence_win_rate numeric NULL,
    evidence_total_net_pnl_usdt numeric NULL,
    evidence_average_net_pnl_usdt numeric NULL,
    evidence_average_mfe_r numeric NULL,
    evidence_average_mae_r numeric NULL,
    evidence_drawdown_usdt numeric NULL,
    positive_historical_evidence boolean NOT NULL,
    operator_review_required boolean NOT NULL DEFAULT true
        CHECK (operator_review_required = true),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    strategy_promotion_allowed boolean NOT NULL DEFAULT false
        CHECK (strategy_promotion_allowed = false),
    demo_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (demo_activation_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    PRIMARY KEY (snapshot_id, evidence_rank),
    UNIQUE (snapshot_id, symbol)
);

CREATE INDEX IF NOT EXISTS astra_bybit_strategy_evidence_observed_idx_v111
    ON astra_bybit_strategy_evidence_snapshot_v111(observed_at DESC, evidence_snapshot_id);
CREATE INDEX IF NOT EXISTS astra_bybit_live_opportunity_observed_idx_v111
    ON astra_bybit_live_opportunity_snapshot_v111(observed_at DESC, snapshot_id);
CREATE INDEX IF NOT EXISTS astra_bybit_live_opportunity_symbol_idx_v111
    ON astra_bybit_live_opportunity_candidate_v111(symbol, snapshot_id, evidence_rank);
CREATE INDEX IF NOT EXISTS astra_bybit_live_opportunity_qualified_idx_v111
    ON astra_bybit_live_opportunity_candidate_v111(snapshot_id, evidence_rank)
    WHERE qualification_state IN (
        'QUALIFIED_POSITIVE_EVIDENCE',
        'QUALIFIED_MIXED_EVIDENCE'
    );

CREATE OR REPLACE FUNCTION astra_reject_bybit_live_evidence_mutation_v111()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit live evidence registry v111 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_strategy_evidence_append_only_v111
    ON astra_bybit_strategy_evidence_snapshot_v111;
CREATE TRIGGER astra_bybit_strategy_evidence_append_only_v111
BEFORE UPDATE OR DELETE ON astra_bybit_strategy_evidence_snapshot_v111
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_live_evidence_mutation_v111();

DROP TRIGGER IF EXISTS astra_bybit_live_opportunity_snapshot_append_only_v111
    ON astra_bybit_live_opportunity_snapshot_v111;
CREATE TRIGGER astra_bybit_live_opportunity_snapshot_append_only_v111
BEFORE UPDATE OR DELETE ON astra_bybit_live_opportunity_snapshot_v111
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_live_evidence_mutation_v111();

DROP TRIGGER IF EXISTS astra_bybit_live_opportunity_candidate_append_only_v111
    ON astra_bybit_live_opportunity_candidate_v111;
CREATE TRIGGER astra_bybit_live_opportunity_candidate_append_only_v111
BEFORE UPDATE OR DELETE ON astra_bybit_live_opportunity_candidate_v111
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_live_evidence_mutation_v111();

REVOKE ALL ON astra_bybit_strategy_evidence_snapshot_v111 FROM PUBLIC;
REVOKE ALL ON astra_bybit_live_opportunity_snapshot_v111 FROM PUBLIC;
REVOKE ALL ON astra_bybit_live_opportunity_candidate_v111 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_live_evidence_mutation_v111() FROM PUBLIC;

COMMIT;
