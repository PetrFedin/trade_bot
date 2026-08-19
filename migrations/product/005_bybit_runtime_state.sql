BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_runtime_leases (
    lease_name text PRIMARY KEY,
    owner_token_sha256 char(64) NOT NULL,
    owner_process_id bigint NOT NULL CHECK (owner_process_id > 0),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    acquired_at timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    active boolean NOT NULL DEFAULT true,
    CHECK (expires_at >= heartbeat_at)
);

CREATE TABLE IF NOT EXISTS astra_bybit_trades (
    entry_order_link_id text PRIMARY KEY,
    symbol text NOT NULL CHECK (symbol <> '' AND symbol = upper(symbol)),
    side text NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    entry_price numeric NOT NULL CHECK (entry_price > 0),
    initial_quantity numeric NOT NULL CHECK (initial_quantity > 0),
    current_quantity numeric CHECK (current_quantity IS NULL OR current_quantity >= 0),
    stop_fraction numeric NOT NULL CHECK (stop_fraction > 0),
    state_payload jsonb NOT NULL,
    revision_sha256 char(64) NOT NULL,
    lifecycle_state text NOT NULL CHECK (lifecycle_state IN ('ACTIVE', 'CLOSED')),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    closed_at timestamptz,
    CHECK (
        (lifecycle_state = 'ACTIVE' AND closed_at IS NULL)
        OR (lifecycle_state = 'CLOSED' AND closed_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_astra_bybit_single_active_trade
    ON astra_bybit_trades (lifecycle_state)
    WHERE lifecycle_state = 'ACTIVE';

CREATE TABLE IF NOT EXISTS astra_bybit_entry_provenance (
    entry_order_link_id text PRIMARY KEY,
    record_sha256 char(64) NOT NULL,
    envelope_text text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_bybit_terminal_evidence (
    entry_order_link_id text PRIMARY KEY,
    checkpoint_revision char(64) NOT NULL,
    record_sha256 char(64) NOT NULL,
    envelope_text text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_bybit_session_risk_ledger (
    ledger_key text PRIMARY KEY,
    opening_equity_usdt numeric NOT NULL CHECK (opening_equity_usdt > 0),
    revision_sha256 char(64) NOT NULL,
    envelope_text text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (ledger_key = 'default')
);

CREATE TABLE IF NOT EXISTS astra_bybit_runtime_events (
    event_id text PRIMARY KEY,
    lease_name text NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    entry_order_link_id text,
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_astra_bybit_runtime_events_trade_time
    ON astra_bybit_runtime_events (entry_order_link_id, occurred_at)
    WHERE entry_order_link_id IS NOT NULL;

CREATE OR REPLACE FUNCTION astra_bybit_runtime_events_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_bybit_runtime_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_runtime_events_no_update_delete
    ON astra_bybit_runtime_events;
CREATE TRIGGER astra_bybit_runtime_events_no_update_delete
BEFORE UPDATE OR DELETE ON astra_bybit_runtime_events
FOR EACH ROW EXECUTE FUNCTION astra_bybit_runtime_events_append_only();

CREATE OR REPLACE FUNCTION astra_bybit_immutable_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_bybit immutable evidence cannot be updated or deleted';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_entry_provenance_no_update_delete
    ON astra_bybit_entry_provenance;
CREATE TRIGGER astra_bybit_entry_provenance_no_update_delete
BEFORE UPDATE OR DELETE ON astra_bybit_entry_provenance
FOR EACH ROW EXECUTE FUNCTION astra_bybit_immutable_evidence();

DROP TRIGGER IF EXISTS astra_bybit_terminal_evidence_no_update_delete
    ON astra_bybit_terminal_evidence;
CREATE TRIGGER astra_bybit_terminal_evidence_no_update_delete
BEFORE UPDATE OR DELETE ON astra_bybit_terminal_evidence
FOR EACH ROW EXECUTE FUNCTION astra_bybit_immutable_evidence();

COMMIT;
