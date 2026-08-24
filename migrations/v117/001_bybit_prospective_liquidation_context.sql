BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_shadow_liquidation_context_v117 (
    context_id text PRIMARY KEY CHECK (context_id ~ '^[0-9a-f]{64}$'),
    seed_id text NOT NULL UNIQUE
        REFERENCES astra_bybit_shadow_seed_v112(seed_id) ON DELETE RESTRICT,
    source_snapshot_id text NOT NULL
        REFERENCES astra_bybit_live_opportunity_snapshot_v111(snapshot_id) ON DELETE RESTRICT,
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    side text NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    signal_available_at timestamptz NOT NULL,
    coverage_window_start_at timestamptz NOT NULL,
    coverage_subscription_id text NULL
        REFERENCES astra_bybit_liquidation_subscription_v116(subscription_id) ON DELETE RESTRICT,
    coverage_qualified boolean NOT NULL,
    coverage_reason_codes jsonb NOT NULL,
    coverage_start_status_at timestamptz NULL,
    coverage_end_status_at timestamptz NULL,
    maximum_status_age_seconds integer NOT NULL
        CHECK (maximum_status_age_seconds BETWEEN 20 AND 300),
    evaluated_at timestamptz NOT NULL,
    context_json jsonb NOT NULL,
    prospective boolean NOT NULL DEFAULT true CHECK (prospective = true),
    liquidation_feature_used_for_source_ranking boolean NOT NULL DEFAULT false
        CHECK (liquidation_feature_used_for_source_ranking = false),
    parameter_retuning_performed boolean NOT NULL DEFAULT false
        CHECK (parameter_retuning_performed = false),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    strategy_promotion_allowed boolean NOT NULL DEFAULT false
        CHECK (strategy_promotion_allowed = false),
    demo_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (demo_activation_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    CHECK (coverage_window_start_at = signal_available_at - interval '60 minutes'),
    CHECK (evaluated_at >= signal_available_at + interval '60 seconds'),
    CHECK (
        (coverage_qualified = true
            AND coverage_subscription_id IS NOT NULL
            AND coverage_start_status_at IS NOT NULL
            AND coverage_end_status_at IS NOT NULL
            AND coverage_reason_codes = '[]'::jsonb)
        OR
        (coverage_qualified = false AND jsonb_array_length(coverage_reason_codes) >= 1)
    )
);

CREATE TABLE IF NOT EXISTS astra_bybit_shadow_liquidation_window_v117 (
    context_id text NOT NULL
        REFERENCES astra_bybit_shadow_liquidation_context_v117(context_id) ON DELETE RESTRICT,
    window_minutes integer NOT NULL CHECK (window_minutes IN (5, 15, 60)),
    window_start_at timestamptz NOT NULL,
    window_end_at timestamptz NOT NULL,
    event_count integer NULL CHECK (event_count IS NULL OR event_count >= 0),
    long_liquidation_count integer NULL
        CHECK (long_liquidation_count IS NULL OR long_liquidation_count >= 0),
    short_liquidation_count integer NULL
        CHECK (short_liquidation_count IS NULL OR short_liquidation_count >= 0),
    long_estimated_notional_usdt numeric NULL
        CHECK (long_estimated_notional_usdt IS NULL OR long_estimated_notional_usdt >= 0),
    short_estimated_notional_usdt numeric NULL
        CHECK (short_estimated_notional_usdt IS NULL OR short_estimated_notional_usdt >= 0),
    total_estimated_notional_usdt numeric NULL
        CHECK (total_estimated_notional_usdt IS NULL OR total_estimated_notional_usdt >= 0),
    long_minus_short_estimated_notional_usdt numeric NULL,
    normalized_long_minus_short_imbalance numeric NULL
        CHECK (
            normalized_long_minus_short_imbalance IS NULL
            OR normalized_long_minus_short_imbalance BETWEEN -1 AND 1
        ),
    largest_event_estimated_notional_usdt numeric NULL
        CHECK (
            largest_event_estimated_notional_usdt IS NULL
            OR largest_event_estimated_notional_usdt >= 0
        ),
    first_event_at timestamptz NULL,
    last_event_at timestamptz NULL,
    known_zero boolean NOT NULL,
    PRIMARY KEY (context_id, window_minutes),
    CHECK (window_end_at > window_start_at),
    CHECK (
        (event_count IS NULL
            AND long_liquidation_count IS NULL
            AND short_liquidation_count IS NULL
            AND long_estimated_notional_usdt IS NULL
            AND short_estimated_notional_usdt IS NULL
            AND total_estimated_notional_usdt IS NULL
            AND long_minus_short_estimated_notional_usdt IS NULL
            AND normalized_long_minus_short_imbalance IS NULL
            AND largest_event_estimated_notional_usdt IS NULL
            AND first_event_at IS NULL
            AND last_event_at IS NULL
            AND known_zero = false)
        OR
        (event_count IS NOT NULL
            AND long_liquidation_count IS NOT NULL
            AND short_liquidation_count IS NOT NULL
            AND long_estimated_notional_usdt IS NOT NULL
            AND short_estimated_notional_usdt IS NOT NULL
            AND total_estimated_notional_usdt IS NOT NULL
            AND long_minus_short_estimated_notional_usdt IS NOT NULL
            AND normalized_long_minus_short_imbalance IS NOT NULL
            AND largest_event_estimated_notional_usdt IS NOT NULL
            AND event_count > 0
            AND known_zero = false)
        OR
        (event_count = 0
            AND long_liquidation_count = 0
            AND short_liquidation_count = 0
            AND long_estimated_notional_usdt = 0
            AND short_estimated_notional_usdt = 0
            AND total_estimated_notional_usdt = 0
            AND long_minus_short_estimated_notional_usdt = 0
            AND normalized_long_minus_short_imbalance = 0
            AND largest_event_estimated_notional_usdt = 0
            AND first_event_at IS NULL
            AND last_event_at IS NULL
            AND known_zero = true)
    ),
    CHECK (
        event_count IS NULL
        OR event_count = long_liquidation_count + short_liquidation_count
    ),
    CHECK (
        total_estimated_notional_usdt IS NULL
        OR total_estimated_notional_usdt =
            long_estimated_notional_usdt + short_estimated_notional_usdt
    ),
    CHECK (
        long_minus_short_estimated_notional_usdt IS NULL
        OR long_minus_short_estimated_notional_usdt =
            long_estimated_notional_usdt - short_estimated_notional_usdt
    )
);

CREATE INDEX IF NOT EXISTS astra_bybit_shadow_liquidation_context_signal_idx_v117
    ON astra_bybit_shadow_liquidation_context_v117(
        signal_available_at, symbol, side, context_id
    );
CREATE INDEX IF NOT EXISTS astra_bybit_shadow_liquidation_context_coverage_idx_v117
    ON astra_bybit_shadow_liquidation_context_v117(
        coverage_qualified, signal_available_at, symbol
    );

CREATE OR REPLACE FUNCTION astra_reject_bybit_shadow_liquidation_mutation_v117()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit prospective liquidation context v117 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_shadow_liquidation_context_append_only_v117
    ON astra_bybit_shadow_liquidation_context_v117;
CREATE TRIGGER astra_bybit_shadow_liquidation_context_append_only_v117
BEFORE UPDATE OR DELETE ON astra_bybit_shadow_liquidation_context_v117
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_shadow_liquidation_mutation_v117();

DROP TRIGGER IF EXISTS astra_bybit_shadow_liquidation_window_append_only_v117
    ON astra_bybit_shadow_liquidation_window_v117;
CREATE TRIGGER astra_bybit_shadow_liquidation_window_append_only_v117
BEFORE UPDATE OR DELETE ON astra_bybit_shadow_liquidation_window_v117
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_shadow_liquidation_mutation_v117();

REVOKE ALL ON astra_bybit_shadow_liquidation_context_v117 FROM PUBLIC;
REVOKE ALL ON astra_bybit_shadow_liquidation_window_v117 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_shadow_liquidation_mutation_v117() FROM PUBLIC;

COMMIT;
