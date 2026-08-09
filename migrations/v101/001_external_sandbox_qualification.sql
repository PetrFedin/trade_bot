BEGIN;

CREATE SCHEMA IF NOT EXISTS astra_platform;

CREATE TABLE IF NOT EXISTS astra_platform.sandbox_qualification_runs_v101 (
    qualification_id text PRIMARY KEY,
    generation bigint NOT NULL CHECK (generation > 0),
    account_id text NOT NULL,
    symbol text NOT NULL CHECK (symbol = upper(symbol)),
    state text NOT NULL,
    plan_digest char(64) NOT NULL,
    probe_evidence_digest char(64),
    approval_id text,
    approval_nonce_digest char(64),
    credential_fingerprint char(16),
    broker_order_id text,
    filled_quantity numeric(38, 18) NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    kill_switch_engaged boolean NOT NULL DEFAULT false,
    read_only_probe_verified boolean NOT NULL DEFAULT false,
    paper_round_trip_verified boolean NOT NULL DEFAULT false,
    cleanup_verified boolean NOT NULL DEFAULT false,
    external_order_routing_allowed boolean NOT NULL DEFAULT false CHECK (external_order_routing_allowed = false),
    live_trading_allowed boolean NOT NULL DEFAULT false CHECK (live_trading_allowed = false),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sandbox_qualification_approval_v101
    ON astra_platform.sandbox_qualification_runs_v101 (approval_id)
    WHERE approval_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_sandbox_qualification_nonce_v101
    ON astra_platform.sandbox_qualification_runs_v101 (approval_nonce_digest)
    WHERE approval_nonce_digest IS NOT NULL;

CREATE TABLE IF NOT EXISTS astra_platform.sandbox_qualification_events_v101 (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    qualification_id text NOT NULL REFERENCES astra_platform.sandbox_qualification_runs_v101(qualification_id),
    event_type text NOT NULL,
    from_state text NOT NULL,
    to_state text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    occurred_at timestamptz NOT NULL,
    attributes jsonb NOT NULL,
    previous_digest char(64) NOT NULL,
    event_digest char(64) NOT NULL UNIQUE,
    CHECK (COALESCE((attributes->>'external_order_routing_allowed')::boolean, false) = false),
    CHECK (COALESCE((attributes->>'live_trading_allowed')::boolean, false) = false)
);

CREATE TABLE IF NOT EXISTS astra_platform.sandbox_kill_switch_v101 (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    engaged boolean NOT NULL DEFAULT false,
    reason text NOT NULL DEFAULT '',
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    engaged_at timestamptz,
    status_digest char(64) NOT NULL,
    CHECK ((engaged AND engaged_at IS NOT NULL) OR (NOT engaged AND engaged_at IS NULL))
);

CREATE OR REPLACE FUNCTION astra_platform.reject_sandbox_event_mutation_v101()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'sandbox qualification event log is append-only';
END;
$$;

DROP TRIGGER IF EXISTS sandbox_events_no_update_v101 ON astra_platform.sandbox_qualification_events_v101;
CREATE TRIGGER sandbox_events_no_update_v101
BEFORE UPDATE OR DELETE ON astra_platform.sandbox_qualification_events_v101
FOR EACH ROW EXECUTE FUNCTION astra_platform.reject_sandbox_event_mutation_v101();

REVOKE UPDATE, DELETE, TRUNCATE ON astra_platform.sandbox_qualification_events_v101 FROM PUBLIC;
REVOKE ALL ON astra_platform.sandbox_kill_switch_v101 FROM PUBLIC;

COMMIT;
