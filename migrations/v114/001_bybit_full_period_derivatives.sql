BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_derivatives_day_v114 (
    attempt_id text PRIMARY KEY CHECK (attempt_id ~ '^[0-9a-f]{64}$'),
    source_series text NOT NULL CHECK (
        source_series IN ('OPEN_INTEREST', 'ACCOUNT_RATIO', 'FUNDING')
    ),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    archive_date date NOT NULL,
    query_start_at timestamptz NOT NULL,
    query_end_at timestamptz NOT NULL CHECK (query_end_at > query_start_at),
    state text NOT NULL CHECK (state IN ('COMPLETE', 'UNAVAILABLE')),
    error_code text NULL,
    retry_after timestamptz NULL,
    point_count integer NULL CHECK (point_count IS NULL OR point_count >= 0),
    expected_point_count integer NULL CHECK (
        expected_point_count IS NULL OR expected_point_count >= 0
    ),
    missing_point_count integer NULL CHECK (
        missing_point_count IS NULL OR missing_point_count >= 0
    ),
    extra_point_count integer NULL CHECK (
        extra_point_count IS NULL OR extra_point_count >= 0
    ),
    exact_grid_required boolean NULL,
    query_window_complete boolean NULL,
    point_fingerprint text NULL CHECK (
        point_fingerprint IS NULL OR point_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    observed_at timestamptz NOT NULL,
    source text NOT NULL DEFAULT 'BYBIT_V5_PUBLIC_DERIVATIVES_HISTORY'
        CHECK (source = 'BYBIT_V5_PUBLIC_DERIVATIVES_HISTORY'),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    demo_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (demo_activation_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    CHECK ((query_start_at AT TIME ZONE 'UTC')::date = archive_date),
    CHECK (
        (state = 'COMPLETE'
            AND error_code IS NULL
            AND retry_after IS NULL
            AND point_count IS NOT NULL
            AND missing_point_count = 0
            AND extra_point_count = 0
            AND exact_grid_required IS NOT NULL
            AND query_window_complete = true
            AND point_fingerprint IS NOT NULL)
        OR
        (state = 'UNAVAILABLE'
            AND error_code IS NOT NULL
            AND retry_after IS NOT NULL
            AND point_count IS NULL
            AND expected_point_count IS NULL
            AND missing_point_count IS NULL
            AND extra_point_count IS NULL
            AND exact_grid_required IS NULL
            AND query_window_complete IS NULL
            AND point_fingerprint IS NULL)
    ),
    CHECK (
        (source_series IN ('OPEN_INTEREST', 'ACCOUNT_RATIO')
            AND (state <> 'COMPLETE' OR expected_point_count IS NOT NULL))
        OR
        (source_series = 'FUNDING'
            AND (state <> 'COMPLETE' OR expected_point_count IS NULL))
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS astra_bybit_derivatives_day_complete_uq_v114
    ON astra_bybit_derivatives_day_v114(source_series, symbol, archive_date)
    WHERE state = 'COMPLETE';
CREATE INDEX IF NOT EXISTS astra_bybit_derivatives_day_retry_idx_v114
    ON astra_bybit_derivatives_day_v114(
        source_series, symbol, archive_date, retry_after DESC, observed_at DESC
    )
    WHERE state = 'UNAVAILABLE';

CREATE TABLE IF NOT EXISTS astra_bybit_open_interest_v114 (
    point_id text PRIMARY KEY CHECK (point_id ~ '^[0-9a-f]{64}$'),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    timestamp_at timestamptz NOT NULL,
    open_interest numeric NOT NULL CHECK (open_interest >= 0),
    single_open_interest numeric NULL CHECK (
        single_open_interest IS NULL OR single_open_interest >= 0
    ),
    source text NOT NULL DEFAULT 'BYBIT_V5_PUBLIC_OPEN_INTEREST'
        CHECK (source = 'BYBIT_V5_PUBLIC_OPEN_INTEREST'),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    UNIQUE (symbol, timestamp_at)
);

CREATE TABLE IF NOT EXISTS astra_bybit_account_ratio_v114 (
    point_id text PRIMARY KEY CHECK (point_id ~ '^[0-9a-f]{64}$'),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    timestamp_at timestamptz NOT NULL,
    buy_ratio numeric NOT NULL CHECK (buy_ratio >= 0 AND buy_ratio <= 1),
    sell_ratio numeric NOT NULL CHECK (sell_ratio >= 0 AND sell_ratio <= 1),
    source text NOT NULL DEFAULT 'BYBIT_V5_PUBLIC_ACCOUNT_RATIO'
        CHECK (source = 'BYBIT_V5_PUBLIC_ACCOUNT_RATIO'),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    UNIQUE (symbol, timestamp_at),
    CHECK (abs((buy_ratio + sell_ratio) - 1) <= 0.02)
);

CREATE TABLE IF NOT EXISTS astra_bybit_funding_rate_v114 (
    point_id text PRIMARY KEY CHECK (point_id ~ '^[0-9a-f]{64}$'),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    timestamp_at timestamptz NOT NULL,
    funding_rate numeric NOT NULL,
    source text NOT NULL DEFAULT 'BYBIT_V5_PUBLIC_FUNDING_HISTORY'
        CHECK (source = 'BYBIT_V5_PUBLIC_FUNDING_HISTORY'),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    UNIQUE (symbol, timestamp_at)
);

CREATE INDEX IF NOT EXISTS astra_bybit_open_interest_symbol_time_idx_v114
    ON astra_bybit_open_interest_v114(symbol, timestamp_at);
CREATE INDEX IF NOT EXISTS astra_bybit_account_ratio_symbol_time_idx_v114
    ON astra_bybit_account_ratio_v114(symbol, timestamp_at);
CREATE INDEX IF NOT EXISTS astra_bybit_funding_rate_symbol_time_idx_v114
    ON astra_bybit_funding_rate_v114(symbol, timestamp_at);

CREATE OR REPLACE FUNCTION astra_reject_bybit_full_period_derivatives_mutation_v114()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit full-period derivatives registry v114 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_derivatives_day_append_only_v114
    ON astra_bybit_derivatives_day_v114;
CREATE TRIGGER astra_bybit_derivatives_day_append_only_v114
BEFORE UPDATE OR DELETE ON astra_bybit_derivatives_day_v114
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_full_period_derivatives_mutation_v114();

DROP TRIGGER IF EXISTS astra_bybit_open_interest_append_only_v114
    ON astra_bybit_open_interest_v114;
CREATE TRIGGER astra_bybit_open_interest_append_only_v114
BEFORE UPDATE OR DELETE ON astra_bybit_open_interest_v114
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_full_period_derivatives_mutation_v114();

DROP TRIGGER IF EXISTS astra_bybit_account_ratio_append_only_v114
    ON astra_bybit_account_ratio_v114;
CREATE TRIGGER astra_bybit_account_ratio_append_only_v114
BEFORE UPDATE OR DELETE ON astra_bybit_account_ratio_v114
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_full_period_derivatives_mutation_v114();

DROP TRIGGER IF EXISTS astra_bybit_funding_rate_append_only_v114
    ON astra_bybit_funding_rate_v114;
CREATE TRIGGER astra_bybit_funding_rate_append_only_v114
BEFORE UPDATE OR DELETE ON astra_bybit_funding_rate_v114
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_full_period_derivatives_mutation_v114();

REVOKE ALL ON astra_bybit_derivatives_day_v114 FROM PUBLIC;
REVOKE ALL ON astra_bybit_open_interest_v114 FROM PUBLIC;
REVOKE ALL ON astra_bybit_account_ratio_v114 FROM PUBLIC;
REVOKE ALL ON astra_bybit_funding_rate_v114 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_full_period_derivatives_mutation_v114() FROM PUBLIC;

COMMIT;
