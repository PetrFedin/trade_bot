BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_demo_session_start_event_v123 (
    session_start_id text PRIMARY KEY CHECK (session_start_id ~ '^[0-9a-f]{64}$'),
    session_name text NOT NULL UNIQUE
        REFERENCES astra_bybit_demo_session_risk_v122(session_name) ON DELETE RESTRICT,
    operator_id text NOT NULL CHECK (
        length(btrim(operator_id)) BETWEEN 1 AND 128
        AND operator_id = btrim(operator_id)
    ),
    reason text NOT NULL CHECK (
        length(btrim(reason)) BETWEEN 1 AND 1000
        AND reason = btrim(reason)
    ),
    git_sha text NOT NULL CHECK (git_sha ~ '^[0-9a-f]{40}$'),
    preflight_record_sha256 text NOT NULL
        CHECK (preflight_record_sha256 ~ '^[0-9a-f]{64}$'),
    initial_ledger_revision_sha256 text NOT NULL
        CHECK (initial_ledger_revision_sha256 ~ '^[0-9a-f]{64}$'),
    canonical_record text NOT NULL CHECK (canonical_record <> ''),
    immutable_record boolean NOT NULL DEFAULT true CHECK (immutable_record = true),
    fixed_egress_required boolean NOT NULL DEFAULT true CHECK (fixed_egress_required = true),
    order_write_performed boolean NOT NULL DEFAULT false CHECK (order_write_performed = false),
    order_writes_supported boolean NOT NULL DEFAULT false CHECK (order_writes_supported = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    started_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (created_at >= started_at),
    CHECK (
        canonical_record::jsonb ->> 'schema' = 'BYBIT_DEMO_SESSION_START_AUDIT_V1'
        AND canonical_record::jsonb ->> 'session_name' = session_name
        AND canonical_record::jsonb ->> 'operator_id' = operator_id
        AND canonical_record::jsonb ->> 'reason' = reason
        AND canonical_record::jsonb ->> 'git_sha' = git_sha
        AND canonical_record::jsonb ->> 'preflight_record_sha256' = preflight_record_sha256
        AND canonical_record::jsonb ->> 'initial_ledger_revision_sha256'
            = initial_ledger_revision_sha256
        AND (canonical_record::jsonb ->> 'fixed_egress_required')::boolean = true
        AND (canonical_record::jsonb ->> 'order_write_performed')::boolean = false
        AND (canonical_record::jsonb ->> 'order_writes_supported')::boolean = false
        AND (
            canonical_record::jsonb ->> 'live_mainnet_order_routing_allowed'
        )::boolean = false
        AND (canonical_record::jsonb ->> 'started_at')::timestamptz = started_at
    )
);

CREATE OR REPLACE FUNCTION astra_reject_bybit_demo_session_start_mutation_v123()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit Demo session-start provenance v123 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_demo_session_start_append_only_v123
    ON astra_bybit_demo_session_start_event_v123;
CREATE TRIGGER astra_bybit_demo_session_start_append_only_v123
BEFORE UPDATE OR DELETE ON astra_bybit_demo_session_start_event_v123
FOR EACH ROW
EXECUTE FUNCTION astra_reject_bybit_demo_session_start_mutation_v123();

DROP TRIGGER IF EXISTS astra_bybit_demo_session_start_no_truncate_v123
    ON astra_bybit_demo_session_start_event_v123;
CREATE TRIGGER astra_bybit_demo_session_start_no_truncate_v123
BEFORE TRUNCATE ON astra_bybit_demo_session_start_event_v123
FOR EACH STATEMENT
EXECUTE FUNCTION astra_reject_bybit_demo_session_start_mutation_v123();

REVOKE ALL ON astra_bybit_demo_session_start_event_v123 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_demo_session_start_mutation_v123()
    FROM PUBLIC;

COMMIT;
