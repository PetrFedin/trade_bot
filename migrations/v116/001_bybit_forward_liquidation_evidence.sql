BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_liquidation_subscription_v116 (
    subscription_id text PRIMARY KEY CHECK (subscription_id ~ '^[0-9a-f]{64}$'),
    source_opportunity_snapshot_id text NOT NULL
        REFERENCES astra_bybit_opportunity_snapshot_v110(snapshot_id) ON DELETE RESTRICT,
    source_snapshot_observed_at timestamptz NOT NULL,
    started_at timestamptz NOT NULL,
    started_at_ms bigint NOT NULL CHECK (started_at_ms >= 0),
    ws_host text NOT NULL CHECK (ws_host = lower(ws_host) AND ws_host <> ''),
    rank_limit integer NOT NULL CHECK (rank_limit BETWEEN 10 AND 50),
    symbol_count integer NOT NULL CHECK (symbol_count BETWEEN 1 AND 50),
    symbols jsonb NOT NULL,
    top10_symbols jsonb NOT NULL,
    source_schema text NOT NULL CHECK (source_schema = 'BYBIT_OPPORTUNITY_REGISTRY_V110'),
    stream_topic_schema text NOT NULL CHECK (stream_topic_schema = 'allLiquidation.{symbol}'),
    forward_only boolean NOT NULL DEFAULT true CHECK (forward_only = true),
    historical_backfill_available boolean NOT NULL DEFAULT false
        CHECK (historical_backfill_available = false),
    exchange_event_id_available boolean NOT NULL DEFAULT false
        CHECK (exchange_event_id_available = false),
    research_only boolean NOT NULL DEFAULT true CHECK (research_only = true),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    UNIQUE (source_opportunity_snapshot_id, ws_host, started_at_ms)
);

CREATE TABLE IF NOT EXISTS astra_bybit_liquidation_event_v116 (
    event_id text PRIMARY KEY CHECK (event_id ~ '^[0-9a-f]{64}$'),
    first_subscription_id text NOT NULL
        REFERENCES astra_bybit_liquidation_subscription_v116(subscription_id) ON DELETE RESTRICT,
    system_ts_ms bigint NOT NULL CHECK (system_ts_ms >= 0),
    event_time timestamptz NOT NULL,
    event_time_ms bigint NOT NULL CHECK (event_time_ms >= 0),
    bucket_start timestamptz NOT NULL,
    bucket_start_ms bigint NOT NULL CHECK (bucket_start_ms >= 0),
    symbol text NOT NULL CHECK (symbol = upper(symbol) AND symbol <> ''),
    raw_position_side text NOT NULL CHECK (raw_position_side IN ('Buy', 'Sell')),
    liquidated_position_side text NOT NULL
        CHECK (liquidated_position_side IN ('LONG', 'SHORT')),
    quantity_base numeric NOT NULL CHECK (quantity_base > 0),
    bankruptcy_price numeric NOT NULL CHECK (bankruptcy_price > 0),
    estimated_notional_usdt numeric NOT NULL CHECK (estimated_notional_usdt > 0),
    message_ordinal integer NOT NULL CHECK (message_ordinal >= 0),
    dedupe_semantics text NOT NULL DEFAULT 'MESSAGE_TS_EVENT_FIELDS_ORDINAL'
        CHECK (dedupe_semantics = 'MESSAGE_TS_EVENT_FIELDS_ORDINAL'),
    exchange_event_id_available boolean NOT NULL DEFAULT false
        CHECK (exchange_event_id_available = false),
    historical_backfill_available boolean NOT NULL DEFAULT false
        CHECK (historical_backfill_available = false),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    received_at timestamptz NOT NULL,
    CHECK (estimated_notional_usdt = quantity_base * bankruptcy_price),
    CHECK (bucket_start_ms = (event_time_ms / 300000) * 300000),
    CHECK (
        (raw_position_side = 'Buy' AND liquidated_position_side = 'LONG')
        OR (raw_position_side = 'Sell' AND liquidated_position_side = 'SHORT')
    )
);

CREATE TABLE IF NOT EXISTS astra_bybit_liquidation_stream_status_v116 (
    status_id text PRIMARY KEY CHECK (status_id ~ '^[0-9a-f]{64}$'),
    subscription_id text NOT NULL
        REFERENCES astra_bybit_liquidation_subscription_v116(subscription_id) ON DELETE RESTRICT,
    connection_epoch text NOT NULL CHECK (connection_epoch ~ '^[0-9a-f]{32}$'),
    observed_at timestamptz NOT NULL,
    observed_at_ms bigint NOT NULL CHECK (observed_at_ms >= 0),
    state text NOT NULL
        CHECK (state IN ('CONNECTING', 'CONNECTED', 'HEARTBEAT', 'DISCONNECTED', 'STOPPED')),
    reason_code text NULL CHECK (reason_code IS NULL OR reason_code ~ '^[A-Za-z0-9_]{1,80}$'),
    public_data_only boolean NOT NULL DEFAULT true CHECK (public_data_only = true),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    UNIQUE (subscription_id, connection_epoch, observed_at_ms, state)
);

CREATE INDEX IF NOT EXISTS astra_bybit_liquidation_event_symbol_time_idx_v116
    ON astra_bybit_liquidation_event_v116(symbol, event_time DESC, event_id);
CREATE INDEX IF NOT EXISTS astra_bybit_liquidation_event_bucket_idx_v116
    ON astra_bybit_liquidation_event_v116(bucket_start, symbol, event_id);
CREATE INDEX IF NOT EXISTS astra_bybit_liquidation_status_subscription_time_idx_v116
    ON astra_bybit_liquidation_stream_status_v116(
        subscription_id, observed_at DESC, status_id
    );

CREATE OR REPLACE VIEW astra_bybit_liquidation_5m_v116 AS
SELECT
    symbol,
    bucket_start,
    bucket_start_ms,
    count(*)::bigint AS event_count,
    count(*) FILTER (WHERE liquidated_position_side = 'LONG')::bigint
        AS long_liquidation_count,
    count(*) FILTER (WHERE liquidated_position_side = 'SHORT')::bigint
        AS short_liquidation_count,
    COALESCE(
        sum(estimated_notional_usdt) FILTER (WHERE liquidated_position_side = 'LONG'),
        0::numeric
    ) AS long_estimated_notional_usdt,
    COALESCE(
        sum(estimated_notional_usdt) FILTER (WHERE liquidated_position_side = 'SHORT'),
        0::numeric
    ) AS short_estimated_notional_usdt,
    sum(estimated_notional_usdt) AS total_estimated_notional_usdt,
    COALESCE(
        sum(estimated_notional_usdt) FILTER (WHERE liquidated_position_side = 'LONG'),
        0::numeric
    ) - COALESCE(
        sum(estimated_notional_usdt) FILTER (WHERE liquidated_position_side = 'SHORT'),
        0::numeric
    ) AS long_minus_short_estimated_notional_usdt,
    (
        COALESCE(
            sum(estimated_notional_usdt) FILTER (WHERE liquidated_position_side = 'LONG'),
            0::numeric
        ) - COALESCE(
            sum(estimated_notional_usdt) FILTER (WHERE liquidated_position_side = 'SHORT'),
            0::numeric
        )
    ) / NULLIF(sum(estimated_notional_usdt), 0::numeric)
        AS normalized_long_minus_short_imbalance,
    max(estimated_notional_usdt) AS largest_event_estimated_notional_usdt,
    min(event_time) AS first_event_at,
    max(event_time) AS last_event_at,
    min(system_ts_ms) AS first_system_ts_ms,
    max(system_ts_ms) AS last_system_ts_ms,
    false AS historical_backfill_available,
    false AS trade_actionable,
    false AS live_mainnet_order_routing_allowed
FROM astra_bybit_liquidation_event_v116
GROUP BY symbol, bucket_start, bucket_start_ms;

CREATE OR REPLACE VIEW astra_bybit_liquidation_subscription_health_v116 AS
SELECT
    subscription_id,
    max(observed_at) AS last_status_at,
    max(observed_at_ms) AS last_status_at_ms,
    (array_agg(state ORDER BY observed_at_ms DESC, status_id DESC))[1] AS last_state,
    count(*) FILTER (WHERE state = 'CONNECTED')::bigint AS connection_count,
    count(*) FILTER (WHERE state = 'DISCONNECTED')::bigint AS disconnect_count,
    count(*) FILTER (WHERE state = 'HEARTBEAT')::bigint AS heartbeat_count,
    false AS historical_backfill_available,
    false AS trade_actionable,
    false AS live_mainnet_order_routing_allowed
FROM astra_bybit_liquidation_stream_status_v116
GROUP BY subscription_id;

CREATE OR REPLACE FUNCTION astra_reject_bybit_liquidation_mutation_v116()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit forward liquidation evidence v116 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_liquidation_subscription_append_only_v116
    ON astra_bybit_liquidation_subscription_v116;
CREATE TRIGGER astra_bybit_liquidation_subscription_append_only_v116
BEFORE UPDATE OR DELETE ON astra_bybit_liquidation_subscription_v116
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_liquidation_mutation_v116();

DROP TRIGGER IF EXISTS astra_bybit_liquidation_event_append_only_v116
    ON astra_bybit_liquidation_event_v116;
CREATE TRIGGER astra_bybit_liquidation_event_append_only_v116
BEFORE UPDATE OR DELETE ON astra_bybit_liquidation_event_v116
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_liquidation_mutation_v116();

DROP TRIGGER IF EXISTS astra_bybit_liquidation_status_append_only_v116
    ON astra_bybit_liquidation_stream_status_v116;
CREATE TRIGGER astra_bybit_liquidation_status_append_only_v116
BEFORE UPDATE OR DELETE ON astra_bybit_liquidation_stream_status_v116
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_liquidation_mutation_v116();

REVOKE ALL ON astra_bybit_liquidation_subscription_v116 FROM PUBLIC;
REVOKE ALL ON astra_bybit_liquidation_event_v116 FROM PUBLIC;
REVOKE ALL ON astra_bybit_liquidation_stream_status_v116 FROM PUBLIC;
REVOKE ALL ON astra_bybit_liquidation_5m_v116 FROM PUBLIC;
REVOKE ALL ON astra_bybit_liquidation_subscription_health_v116 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_liquidation_mutation_v116() FROM PUBLIC;

COMMIT;
