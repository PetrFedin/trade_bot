BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_demo_runtime_lease_v119 (
    lease_name text PRIMARY KEY
        CHECK (lease_name = 'CANONICAL_DEMO_TRADING_RUNTIME'),
    owner_token text NOT NULL UNIQUE CHECK (owner_token ~ '^[0-9a-f]{64}$'),
    created_time_ms bigint NOT NULL CHECK (created_time_ms >= 0),
    process_id integer NOT NULL CHECK (process_id > 0),
    automatic_stale_takeover_allowed boolean NOT NULL DEFAULT false
        CHECK (automatic_stale_takeover_allowed = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_bybit_demo_active_excursion_v119 (
    checkpoint_name text PRIMARY KEY CHECK (checkpoint_name = 'ACTIVE'),
    entry_order_link_id text NOT NULL
        CHECK (entry_order_link_id LIKE 'ASTRA-DEMO-%'),
    revision text NOT NULL CHECK (revision ~ '^[0-9a-f]{64}$'),
    state_json jsonb NOT NULL,
    diagnostics_only boolean NOT NULL DEFAULT true CHECK (diagnostics_only = true),
    exit_threshold_retuning_allowed boolean NOT NULL DEFAULT false
        CHECK (exit_threshold_retuning_allowed = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS astra_bybit_demo_active_excursion_identity_idx_v119
    ON astra_bybit_demo_active_excursion_v119(entry_order_link_id, revision);

REVOKE ALL ON astra_bybit_demo_runtime_lease_v119 FROM PUBLIC;
REVOKE ALL ON astra_bybit_demo_active_excursion_v119 FROM PUBLIC;

COMMIT;
