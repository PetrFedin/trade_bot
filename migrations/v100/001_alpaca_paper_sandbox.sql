BEGIN;

CREATE SCHEMA IF NOT EXISTS astra_v100;

CREATE TABLE IF NOT EXISTS astra_v100.paper_adapter_qualifications (
    qualification_id uuid PRIMARY KEY,
    provider text NOT NULL CHECK (provider = 'alpaca-paper'),
    credentials_fingerprint text NOT NULL CHECK (length(credentials_fingerprint) = 16),
    rest_base_url text NOT NULL CHECK (rest_base_url = 'https://paper-api.alpaca.markets'),
    stream_url text NOT NULL CHECK (stream_url = 'wss://paper-api.alpaca.markets/stream'),
    account_id text NOT NULL,
    account_status text NOT NULL,
    captured_at timestamptz NOT NULL,
    ready boolean NOT NULL,
    reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence_digest text NOT NULL CHECK (length(evidence_digest) = 64),
    external_order_routing_allowed boolean NOT NULL DEFAULT false CHECK (NOT external_order_routing_allowed),
    live_trading_allowed boolean NOT NULL DEFAULT false CHECK (NOT live_trading_allowed)
);

CREATE TABLE IF NOT EXISTS astra_v100.paper_stream_cursors (
    stream_name text PRIMARY KEY CHECK (stream_name = 'trade_updates'),
    generation bigint NOT NULL CHECK (generation > 0),
    state text NOT NULL,
    last_message_at timestamptz,
    last_trade_update_at timestamptz,
    accepted_updates bigint NOT NULL DEFAULT 0 CHECK (accepted_updates >= 0),
    duplicate_updates bigint NOT NULL DEFAULT 0 CHECK (duplicate_updates >= 0),
    last_order_digest text,
    updated_at timestamptz NOT NULL,
    CHECK (last_order_digest IS NULL OR length(last_order_digest) = 64)
);

CREATE TABLE IF NOT EXISTS astra_v100.paper_stream_incidents (
    incident_id uuid PRIMARY KEY,
    generation bigint NOT NULL CHECK (generation > 0),
    reason text NOT NULL,
    occurred_at timestamptz NOT NULL,
    frame_digest text CHECK (frame_digest IS NULL OR length(frame_digest) = 64),
    acknowledged_at timestamptz,
    acknowledged_by text
);

CREATE INDEX IF NOT EXISTS paper_adapter_qualifications_captured_at_idx
    ON astra_v100.paper_adapter_qualifications (captured_at DESC);
CREATE INDEX IF NOT EXISTS paper_stream_incidents_occurred_at_idx
    ON astra_v100.paper_stream_incidents (occurred_at DESC);

REVOKE ALL ON SCHEMA astra_v100 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA astra_v100 FROM PUBLIC;

COMMIT;
