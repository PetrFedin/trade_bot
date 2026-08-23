BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_mainnet_readonly_context_v115 (
    context_snapshot_id text PRIMARY KEY CHECK (context_snapshot_id ~ '^[0-9a-f]{64}$'),
    ranking_snapshot_id text NOT NULL
        REFERENCES astra_bybit_live_opportunity_snapshot_v111(snapshot_id),
    observed_at timestamptz NOT NULL,
    api_host text NOT NULL CHECK (api_host = lower(api_host) AND api_host <> ''),
    api_key_fingerprint_sha256 text NOT NULL
        CHECK (api_key_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
    equity_source text NOT NULL
        CHECK (equity_source = 'BYBIT_MAINNET_READONLY_AVAILABLE_CAPITAL_USD_EQUIVALENT'),
    total_equity_usd numeric NOT NULL CHECK (total_equity_usd >= 0),
    total_wallet_balance_usd numeric NOT NULL,
    total_margin_balance_usd numeric NOT NULL,
    total_available_balance_usd numeric NOT NULL,
    total_perp_upl_usd numeric NOT NULL,
    total_initial_margin_usd numeric NOT NULL CHECK (total_initial_margin_usd >= 0),
    total_maintenance_margin_usd numeric NOT NULL
        CHECK (total_maintenance_margin_usd >= 0),
    sizing_capital_usd_equivalent numeric NULL
        CHECK (sizing_capital_usd_equivalent IS NULL OR sizing_capital_usd_equivalent > 0),
    gross_position_value_usd numeric NOT NULL CHECK (gross_position_value_usd >= 0),
    long_position_value_usd numeric NOT NULL CHECK (long_position_value_usd >= 0),
    short_position_value_usd numeric NOT NULL CHECK (short_position_value_usd >= 0),
    net_position_value_usd numeric NOT NULL,
    open_position_count integer NOT NULL CHECK (open_position_count >= 0),
    position_exposure_complete boolean NOT NULL,
    context_json jsonb NOT NULL,
    read_only_verified boolean NOT NULL DEFAULT true CHECK (read_only_verified = true),
    ip_binding_verified boolean NOT NULL DEFAULT true CHECK (ip_binding_verified = true),
    operator_review_required boolean NOT NULL DEFAULT true
        CHECK (operator_review_required = true),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    order_writes_supported boolean NOT NULL DEFAULT false
        CHECK (order_writes_supported = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    UNIQUE (ranking_snapshot_id, context_snapshot_id),
    CHECK (gross_position_value_usd = long_position_value_usd + short_position_value_usd),
    CHECK (net_position_value_usd = long_position_value_usd - short_position_value_usd),
    CHECK (
        sizing_capital_usd_equivalent IS NULL
        OR sizing_capital_usd_equivalent <= total_equity_usd
    ),
    CHECK (
        sizing_capital_usd_equivalent IS NULL
        OR sizing_capital_usd_equivalent <= total_available_balance_usd
    )
);

CREATE INDEX IF NOT EXISTS astra_bybit_mainnet_readonly_context_ranking_idx_v115
    ON astra_bybit_mainnet_readonly_context_v115(ranking_snapshot_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS astra_bybit_mainnet_readonly_context_key_idx_v115
    ON astra_bybit_mainnet_readonly_context_v115(
        api_key_fingerprint_sha256, observed_at DESC
    );

CREATE OR REPLACE FUNCTION astra_reject_bybit_mainnet_readonly_context_mutation_v115()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit mainnet read-only ranking context v115 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_mainnet_readonly_context_append_only_v115
    ON astra_bybit_mainnet_readonly_context_v115;
CREATE TRIGGER astra_bybit_mainnet_readonly_context_append_only_v115
BEFORE UPDATE OR DELETE ON astra_bybit_mainnet_readonly_context_v115
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_mainnet_readonly_context_mutation_v115();

REVOKE ALL ON astra_bybit_mainnet_readonly_context_v115 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_mainnet_readonly_context_mutation_v115()
    FROM PUBLIC;

COMMIT;
