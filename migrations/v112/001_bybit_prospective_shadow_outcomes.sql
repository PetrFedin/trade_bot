BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_shadow_seed_v112 (
    seed_id text PRIMARY KEY CHECK (seed_id ~ '^[0-9a-f]{64}$'),
    source_snapshot_id text NOT NULL
        REFERENCES astra_bybit_live_opportunity_snapshot_v111(snapshot_id) ON DELETE RESTRICT,
    source_evidence_rank integer NOT NULL CHECK (source_evidence_rank BETWEEN 1 AND 50),
    source_market_rank integer NOT NULL CHECK (source_market_rank BETWEEN 1 AND 50),
    source_qualification_state text NOT NULL CHECK (
        source_qualification_state IN (
            'QUALIFIED_POSITIVE_EVIDENCE',
            'QUALIFIED_MIXED_EVIDENCE',
            'NO_SAMPLE_SUFFICIENT_EXACT_CELL',
            'DERIVATIVES_CONTEXT_INCOMPLETE'
        )
    ),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    side text NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    decision_bar_start_at timestamptz NOT NULL,
    signal_available_at timestamptz NOT NULL,
    entry_price numeric NOT NULL CHECK (entry_price > 0),
    stop_price numeric NOT NULL CHECK (stop_price > 0),
    target_price numeric NOT NULL CHECK (target_price > 0),
    planned_notional_usdt numeric NOT NULL CHECK (planned_notional_usdt > 0),
    risk_budget_usdt numeric NOT NULL CHECK (risk_budget_usdt > 0),
    estimated_round_trip_cost_usdt numeric NOT NULL
        CHECK (estimated_round_trip_cost_usdt >= 0),
    target_net_profit_usd numeric NOT NULL CHECK (target_net_profit_usd > 0),
    signal_quality_score numeric NOT NULL,
    seed_json jsonb NOT NULL,
    prospective boolean NOT NULL DEFAULT true CHECK (prospective = true),
    operator_review_required boolean NOT NULL DEFAULT true
        CHECK (operator_review_required = true),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    demo_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (demo_activation_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    CHECK (signal_available_at = decision_bar_start_at + interval '5 minutes'),
    CHECK (
        (side = 'LONG' AND stop_price < entry_price AND entry_price < target_price)
        OR
        (side = 'SHORT' AND target_price < entry_price AND entry_price < stop_price)
    ),
    UNIQUE (source_snapshot_id, symbol)
);

CREATE TABLE IF NOT EXISTS astra_bybit_shadow_outcome_v112 (
    evaluation_id text PRIMARY KEY CHECK (evaluation_id ~ '^[0-9a-f]{64}$'),
    seed_id text NOT NULL
        REFERENCES astra_bybit_shadow_seed_v112(seed_id) ON DELETE RESTRICT,
    source_snapshot_id text NOT NULL
        REFERENCES astra_bybit_live_opportunity_snapshot_v111(snapshot_id) ON DELETE RESTRICT,
    source_qualification_state text NOT NULL CHECK (
        source_qualification_state IN (
            'QUALIFIED_POSITIVE_EVIDENCE',
            'QUALIFIED_MIXED_EVIDENCE',
            'NO_SAMPLE_SUFFICIENT_EXACT_CELL',
            'DERIVATIVES_CONTEXT_INCOMPLETE'
        )
    ),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    side text NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    signal_available_at timestamptz NOT NULL,
    observed_through timestamptz NOT NULL,
    first_touch_state text NOT NULL CHECK (
        first_touch_state IN (
            'TARGET_FIRST',
            'STOP_FIRST',
            'AMBIGUOUS_SAME_BAR',
            'NEITHER',
            'INCOMPLETE'
        )
    ),
    target_hit_at timestamptz NULL,
    stop_hit_at timestamptz NULL,
    first_touch_modeled_net_pnl_usdt numeric NULL,
    mfe_r numeric NULL CHECK (mfe_r IS NULL OR mfe_r >= 0),
    mae_r numeric NULL CHECK (mae_r IS NULL OR mae_r <= 0),
    completed_bar_count integer NOT NULL CHECK (completed_bar_count >= 0),
    horizon_15_complete boolean NOT NULL,
    horizon_60_complete boolean NOT NULL,
    horizon_240_complete boolean NOT NULL,
    final boolean NOT NULL,
    outcome_json jsonb NOT NULL,
    prospective boolean NOT NULL DEFAULT true CHECK (prospective = true),
    operator_review_required boolean NOT NULL DEFAULT true
        CHECK (operator_review_required = true),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    demo_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (demo_activation_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    CHECK (observed_through >= signal_available_at),
    CHECK (final = horizon_240_complete),
    CHECK (
        (first_touch_state = 'TARGET_FIRST' AND target_hit_at IS NOT NULL
            AND stop_hit_at IS NULL AND first_touch_modeled_net_pnl_usdt IS NOT NULL)
        OR
        (first_touch_state = 'STOP_FIRST' AND stop_hit_at IS NOT NULL
            AND target_hit_at IS NULL AND first_touch_modeled_net_pnl_usdt IS NOT NULL)
        OR
        (first_touch_state = 'AMBIGUOUS_SAME_BAR' AND target_hit_at IS NOT NULL
            AND stop_hit_at = target_hit_at AND first_touch_modeled_net_pnl_usdt IS NULL)
        OR
        (first_touch_state IN ('NEITHER', 'INCOMPLETE') AND target_hit_at IS NULL
            AND stop_hit_at IS NULL AND first_touch_modeled_net_pnl_usdt IS NULL)
    ),
    UNIQUE (seed_id, observed_through)
);

CREATE INDEX IF NOT EXISTS astra_bybit_shadow_seed_source_idx_v112
    ON astra_bybit_shadow_seed_v112(source_snapshot_id, source_evidence_rank, symbol);
CREATE INDEX IF NOT EXISTS astra_bybit_shadow_seed_signal_idx_v112
    ON astra_bybit_shadow_seed_v112(signal_available_at, symbol, seed_id);
CREATE INDEX IF NOT EXISTS astra_bybit_shadow_outcome_seed_idx_v112
    ON astra_bybit_shadow_outcome_v112(seed_id, observed_through DESC, evaluation_id);
CREATE INDEX IF NOT EXISTS astra_bybit_shadow_outcome_state_idx_v112
    ON astra_bybit_shadow_outcome_v112(
        source_qualification_state,
        first_touch_state,
        observed_through DESC
    );

CREATE OR REPLACE FUNCTION astra_reject_bybit_shadow_mutation_v112()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit prospective shadow registry v112 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_shadow_seed_append_only_v112
    ON astra_bybit_shadow_seed_v112;
CREATE TRIGGER astra_bybit_shadow_seed_append_only_v112
BEFORE UPDATE OR DELETE ON astra_bybit_shadow_seed_v112
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_shadow_mutation_v112();

DROP TRIGGER IF EXISTS astra_bybit_shadow_outcome_append_only_v112
    ON astra_bybit_shadow_outcome_v112;
CREATE TRIGGER astra_bybit_shadow_outcome_append_only_v112
BEFORE UPDATE OR DELETE ON astra_bybit_shadow_outcome_v112
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_shadow_mutation_v112();

REVOKE ALL ON astra_bybit_shadow_seed_v112 FROM PUBLIC;
REVOKE ALL ON astra_bybit_shadow_outcome_v112 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_shadow_mutation_v112() FROM PUBLIC;

COMMIT;
