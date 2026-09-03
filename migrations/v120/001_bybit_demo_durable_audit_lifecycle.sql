BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_demo_approved_entry_authorization_v120 (
    entry_order_link_id text PRIMARY KEY
        CHECK (entry_order_link_id LIKE 'ASTRA-DEMO-%'),
    approval_id text NOT NULL CHECK (approval_id ~ '^[0-9a-f]{64}$'),
    source_snapshot_id text NOT NULL CHECK (source_snapshot_id ~ '^[0-9a-f]{64}$'),
    source_evidence_rank integer NOT NULL CHECK (source_evidence_rank BETWEEN 1 AND 50),
    source_market_rank integer NOT NULL CHECK (source_market_rank BETWEEN 1 AND 50),
    record_sha256 text NOT NULL CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),
    canonical_record text NOT NULL CHECK (canonical_record <> ''),
    outcome_free boolean NOT NULL DEFAULT true CHECK (outcome_free = true),
    order_submission_supported boolean NOT NULL DEFAULT false
        CHECK (order_submission_supported = false),
    realized_pnl_storage_allowed boolean NOT NULL DEFAULT false
        CHECK (realized_pnl_storage_allowed = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    UNIQUE (approval_id)
);

CREATE TABLE IF NOT EXISTS astra_bybit_demo_entry_provenance_v120 (
    entry_order_link_id text PRIMARY KEY
        CHECK (entry_order_link_id LIKE 'ASTRA-DEMO-%'),
    record_sha256 text NOT NULL CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),
    canonical_record text NOT NULL CHECK (canonical_record <> ''),
    outcome_free boolean NOT NULL DEFAULT true CHECK (outcome_free = true),
    realized_pnl_storage_allowed boolean NOT NULL DEFAULT false
        CHECK (realized_pnl_storage_allowed = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_bybit_demo_terminal_evidence_v120 (
    entry_order_link_id text PRIMARY KEY
        CHECK (entry_order_link_id LIKE 'ASTRA-DEMO-%'),
    checkpoint_revision text NOT NULL CHECK (checkpoint_revision ~ '^[0-9a-f]{64}$'),
    record_sha256 text NOT NULL CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),
    canonical_record text NOT NULL CHECK (canonical_record <> ''),
    fully_reconciled_all_in boolean NOT NULL DEFAULT true
        CHECK (fully_reconciled_all_in = true),
    diagnostics_only boolean NOT NULL DEFAULT true CHECK (diagnostics_only = true),
    exit_threshold_retuning_allowed boolean NOT NULL DEFAULT false
        CHECK (exit_threshold_retuning_allowed = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS astra_bybit_demo_approval_source_idx_v120
    ON astra_bybit_demo_approved_entry_authorization_v120(
        source_snapshot_id,
        source_evidence_rank,
        entry_order_link_id
    );
CREATE INDEX IF NOT EXISTS astra_bybit_demo_terminal_revision_idx_v120
    ON astra_bybit_demo_terminal_evidence_v120(
        checkpoint_revision,
        entry_order_link_id
    );

CREATE OR REPLACE FUNCTION astra_reject_bybit_demo_audit_mutation_v120()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit Demo durable audit lifecycle v120 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_demo_approval_append_only_v120
    ON astra_bybit_demo_approved_entry_authorization_v120;
CREATE TRIGGER astra_bybit_demo_approval_append_only_v120
BEFORE UPDATE OR DELETE ON astra_bybit_demo_approved_entry_authorization_v120
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_demo_audit_mutation_v120();

DROP TRIGGER IF EXISTS astra_bybit_demo_provenance_append_only_v120
    ON astra_bybit_demo_entry_provenance_v120;
CREATE TRIGGER astra_bybit_demo_provenance_append_only_v120
BEFORE UPDATE OR DELETE ON astra_bybit_demo_entry_provenance_v120
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_demo_audit_mutation_v120();

DROP TRIGGER IF EXISTS astra_bybit_demo_terminal_append_only_v120
    ON astra_bybit_demo_terminal_evidence_v120;
CREATE TRIGGER astra_bybit_demo_terminal_append_only_v120
BEFORE UPDATE OR DELETE ON astra_bybit_demo_terminal_evidence_v120
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_demo_audit_mutation_v120();

REVOKE ALL ON astra_bybit_demo_approved_entry_authorization_v120 FROM PUBLIC;
REVOKE ALL ON astra_bybit_demo_entry_provenance_v120 FROM PUBLIC;
REVOKE ALL ON astra_bybit_demo_terminal_evidence_v120 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_demo_audit_mutation_v120() FROM PUBLIC;

COMMIT;
