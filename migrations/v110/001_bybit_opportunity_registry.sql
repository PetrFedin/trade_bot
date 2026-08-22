BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_opportunity_snapshot_v110 (
    snapshot_id text PRIMARY KEY CHECK (snapshot_id ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    observed_at_ms bigint NOT NULL CHECK (observed_at_ms >= 0),
    host text NOT NULL CHECK (host <> ''),
    registry_limit integer NOT NULL CHECK (registry_limit BETWEEN 10 AND 50),
    eligible_symbol_count integer NOT NULL CHECK (eligible_symbol_count >= 0),
    source_instrument_count integer NOT NULL CHECK (source_instrument_count >= eligible_symbol_count),
    source_ticker_count integer NOT NULL CHECK (source_ticker_count >= eligible_symbol_count),
    top10_complete boolean NOT NULL,
    top10_symbols jsonb NOT NULL,
    registry_population_complete boolean NOT NULL,
    blockers jsonb NOT NULL,
    excluded_reasons jsonb NOT NULL,
    snapshot_json jsonb NOT NULL,
    research_only boolean NOT NULL DEFAULT true CHECK (research_only = true),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    strategy_promotion_allowed boolean NOT NULL DEFAULT false
        CHECK (strategy_promotion_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    UNIQUE (host, observed_at_ms)
);

CREATE TABLE IF NOT EXISTS astra_bybit_opportunity_candidate_v110 (
    snapshot_id text NOT NULL
        REFERENCES astra_bybit_opportunity_snapshot_v110(snapshot_id) ON DELETE RESTRICT,
    rank integer NOT NULL CHECK (rank BETWEEN 1 AND 50),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    is_top10 boolean NOT NULL,
    universe_score numeric NOT NULL CHECK (universe_score >= 0 AND universe_score <= 1),
    listing_days integer NOT NULL CHECK (listing_days >= 0),
    turnover_24h_usdt numeric NOT NULL CHECK (turnover_24h_usdt >= 0),
    open_interest_value_usdt numeric NOT NULL CHECK (open_interest_value_usdt >= 0),
    spread_bps numeric NOT NULL CHECK (spread_bps >= 0),
    funding_rate numeric NOT NULL,
    price_24h_fraction numeric NOT NULL,
    turnover_percentile numeric NOT NULL CHECK (turnover_percentile >= 0 AND turnover_percentile <= 1),
    open_interest_percentile numeric NOT NULL
        CHECK (open_interest_percentile >= 0 AND open_interest_percentile <= 1),
    spread_quality_percentile numeric NOT NULL
        CHECK (spread_quality_percentile >= 0 AND spread_quality_percentile <= 1),
    history_percentile numeric NOT NULL CHECK (history_percentile >= 0 AND history_percentile <= 1),
    rank_drivers jsonb NOT NULL,
    signal_side text NOT NULL CHECK (signal_side = 'UNASSIGNED'),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    strategy_promotion_allowed boolean NOT NULL DEFAULT false
        CHECK (strategy_promotion_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    PRIMARY KEY (snapshot_id, rank),
    UNIQUE (snapshot_id, symbol),
    CHECK (is_top10 = (rank <= 10))
);

CREATE INDEX IF NOT EXISTS astra_bybit_opportunity_snapshot_observed_idx_v110
    ON astra_bybit_opportunity_snapshot_v110(observed_at DESC, snapshot_id);
CREATE INDEX IF NOT EXISTS astra_bybit_opportunity_candidate_symbol_idx_v110
    ON astra_bybit_opportunity_candidate_v110(symbol, snapshot_id, rank);
CREATE INDEX IF NOT EXISTS astra_bybit_opportunity_candidate_top10_idx_v110
    ON astra_bybit_opportunity_candidate_v110(snapshot_id, rank)
    WHERE is_top10 = true;

CREATE OR REPLACE FUNCTION astra_reject_bybit_opportunity_mutation_v110()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit opportunity registry v110 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_opportunity_snapshot_append_only_v110
    ON astra_bybit_opportunity_snapshot_v110;
CREATE TRIGGER astra_bybit_opportunity_snapshot_append_only_v110
BEFORE UPDATE OR DELETE ON astra_bybit_opportunity_snapshot_v110
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_opportunity_mutation_v110();

DROP TRIGGER IF EXISTS astra_bybit_opportunity_candidate_append_only_v110
    ON astra_bybit_opportunity_candidate_v110;
CREATE TRIGGER astra_bybit_opportunity_candidate_append_only_v110
BEFORE UPDATE OR DELETE ON astra_bybit_opportunity_candidate_v110
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_opportunity_mutation_v110();

REVOKE ALL ON astra_bybit_opportunity_snapshot_v110 FROM PUBLIC;
REVOKE ALL ON astra_bybit_opportunity_candidate_v110 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_opportunity_mutation_v110() FROM PUBLIC;

COMMIT;
