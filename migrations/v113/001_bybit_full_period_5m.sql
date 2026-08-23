BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_5m_archive_day_v113 (
    attempt_id text PRIMARY KEY CHECK (attempt_id ~ '^[0-9a-f]{64}$'),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    archive_date date NOT NULL,
    source_url text NOT NULL CHECK (source_url LIKE 'https://public.bybit.com/trading/%'),
    state text NOT NULL CHECK (state IN ('COMPLETE', 'UNAVAILABLE')),
    error_code text NULL,
    retry_after timestamptz NULL,
    bar_count integer NULL CHECK (bar_count IS NULL OR bar_count > 0),
    first_bar_at timestamptz NULL,
    last_bar_at timestamptz NULL,
    bar_fingerprint text NULL CHECK (
        bar_fingerprint IS NULL OR bar_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    observed_at timestamptz NOT NULL,
    source text NOT NULL DEFAULT 'BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M'
        CHECK (source = 'BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M'),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    demo_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (demo_activation_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    CHECK (
        (state = 'COMPLETE'
            AND error_code IS NULL
            AND retry_after IS NULL
            AND bar_count IS NOT NULL
            AND first_bar_at IS NOT NULL
            AND last_bar_at IS NOT NULL
            AND bar_fingerprint IS NOT NULL)
        OR
        (state = 'UNAVAILABLE'
            AND error_code IS NOT NULL
            AND retry_after IS NOT NULL
            AND bar_count IS NULL
            AND first_bar_at IS NULL
            AND last_bar_at IS NULL
            AND bar_fingerprint IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS astra_bybit_5m_archive_day_complete_uq_v113
    ON astra_bybit_5m_archive_day_v113(symbol, archive_date)
    WHERE state = 'COMPLETE';

CREATE INDEX IF NOT EXISTS astra_bybit_5m_archive_day_retry_idx_v113
    ON astra_bybit_5m_archive_day_v113(symbol, archive_date, retry_after DESC, observed_at DESC)
    WHERE state = 'UNAVAILABLE';

CREATE TABLE IF NOT EXISTS astra_bybit_5m_bar_v113 (
    bar_id text PRIMARY KEY CHECK (bar_id ~ '^[0-9a-f]{64}$'),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    start_time timestamptz NOT NULL,
    archive_date date NOT NULL,
    open numeric NOT NULL CHECK (open > 0),
    high numeric NOT NULL CHECK (high > 0),
    low numeric NOT NULL CHECK (low > 0),
    close numeric NOT NULL CHECK (close > 0),
    volume numeric NOT NULL CHECK (volume >= 0),
    turnover numeric NOT NULL CHECK (turnover >= 0),
    source text NOT NULL DEFAULT 'BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M'
        CHECK (source = 'BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M'),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    demo_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (demo_activation_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    UNIQUE (symbol, start_time),
    CHECK (high >= open AND high >= close AND high >= low),
    CHECK (low <= open AND low <= close AND low <= high),
    CHECK ((start_time AT TIME ZONE 'UTC')::date = archive_date),
    CHECK (extract(second FROM start_time AT TIME ZONE 'UTC') = 0),
    CHECK ((extract(minute FROM start_time AT TIME ZONE 'UTC')::integer % 5) = 0)
);

CREATE INDEX IF NOT EXISTS astra_bybit_5m_bar_symbol_time_idx_v113
    ON astra_bybit_5m_bar_v113(symbol, start_time);
CREATE INDEX IF NOT EXISTS astra_bybit_5m_bar_archive_day_idx_v113
    ON astra_bybit_5m_bar_v113(symbol, archive_date, start_time);

CREATE OR REPLACE FUNCTION astra_reject_bybit_full_period_5m_mutation_v113()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit full-period 5m registry v113 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_5m_archive_day_append_only_v113
    ON astra_bybit_5m_archive_day_v113;
CREATE TRIGGER astra_bybit_5m_archive_day_append_only_v113
BEFORE UPDATE OR DELETE ON astra_bybit_5m_archive_day_v113
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_full_period_5m_mutation_v113();

DROP TRIGGER IF EXISTS astra_bybit_5m_bar_append_only_v113
    ON astra_bybit_5m_bar_v113;
CREATE TRIGGER astra_bybit_5m_bar_append_only_v113
BEFORE UPDATE OR DELETE ON astra_bybit_5m_bar_v113
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_full_period_5m_mutation_v113();

REVOKE ALL ON astra_bybit_5m_archive_day_v113 FROM PUBLIC;
REVOKE ALL ON astra_bybit_5m_bar_v113 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_full_period_5m_mutation_v113() FROM PUBLIC;

COMMIT;
