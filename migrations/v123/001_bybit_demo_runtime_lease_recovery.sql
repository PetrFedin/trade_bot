BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_demo_runtime_lease_recovery_v123 (
    recovery_seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recovery_id text NOT NULL UNIQUE CHECK (recovery_id ~ '^[0-9a-f]{64}$'),
    lease_name text NOT NULL CHECK (lease_name = 'CANONICAL_DEMO_TRADING_RUNTIME'),
    lease_owner_sha256 text NOT NULL UNIQUE
        CHECK (lease_owner_sha256 ~ '^[0-9a-f]{64}$'),
    lease_created_time_ms bigint NOT NULL CHECK (lease_created_time_ms >= 0),
    lease_process_id bigint NOT NULL CHECK (lease_process_id > 0),
    operator_id text NOT NULL CHECK (btrim(operator_id) <> '' AND length(operator_id) <= 128),
    reason text NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 1000),
    process_stop_evidence text NOT NULL
        CHECK (btrim(process_stop_evidence) <> '' AND length(process_stop_evidence) <= 1000),
    control_event_id text NOT NULL CHECK (control_event_id ~ '^[0-9a-f]{64}$'),
    control_event_kind text NOT NULL CHECK (control_event_kind = 'HALT_NEW_ENTRIES'),
    active_checkpoint_present boolean NOT NULL,
    active_checkpoint_entry_order_link_id_sha256 text NULL CHECK (
        active_checkpoint_entry_order_link_id_sha256 IS NULL
        OR active_checkpoint_entry_order_link_id_sha256 ~ '^[0-9a-f]{64}$'
    ),
    created_at timestamptz NOT NULL,
    immutable_record boolean NOT NULL DEFAULT true CHECK (immutable_record = true),
    order_writes_supported boolean NOT NULL DEFAULT false
        CHECK (order_writes_supported = false),
    automatic_stale_takeover_allowed boolean NOT NULL DEFAULT false
        CHECK (automatic_stale_takeover_allowed = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    CHECK (
        (active_checkpoint_present AND active_checkpoint_entry_order_link_id_sha256 IS NOT NULL)
        OR
        (NOT active_checkpoint_present AND active_checkpoint_entry_order_link_id_sha256 IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS astra_bybit_demo_runtime_lease_recovery_latest_idx_v123
    ON astra_bybit_demo_runtime_lease_recovery_v123(recovery_seq DESC);

CREATE OR REPLACE FUNCTION astra_reject_bybit_demo_runtime_lease_recovery_mutation_v123()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit Demo runtime lease recovery v123 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_demo_runtime_lease_recovery_append_only_v123
    ON astra_bybit_demo_runtime_lease_recovery_v123;
CREATE TRIGGER astra_bybit_demo_runtime_lease_recovery_append_only_v123
BEFORE UPDATE OR DELETE ON astra_bybit_demo_runtime_lease_recovery_v123
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_demo_runtime_lease_recovery_mutation_v123();

DROP TRIGGER IF EXISTS astra_bybit_demo_runtime_lease_recovery_no_truncate_v123
    ON astra_bybit_demo_runtime_lease_recovery_v123;
CREATE TRIGGER astra_bybit_demo_runtime_lease_recovery_no_truncate_v123
BEFORE TRUNCATE ON astra_bybit_demo_runtime_lease_recovery_v123
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_bybit_demo_runtime_lease_recovery_mutation_v123();

-- v121 originally rejected UPDATE/DELETE but a statement-level TRUNCATE could bypass that
-- row trigger. v123 closes the control-journal history gap without rewriting the old migration.
DROP TRIGGER IF EXISTS astra_bybit_demo_control_no_truncate_v123
    ON astra_bybit_demo_control_event_v121;
CREATE TRIGGER astra_bybit_demo_control_no_truncate_v123
BEFORE TRUNCATE ON astra_bybit_demo_control_event_v121
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_bybit_demo_control_mutation_v121();

REVOKE ALL ON astra_bybit_demo_runtime_lease_recovery_v123 FROM PUBLIC;
REVOKE ALL ON SEQUENCE astra_bybit_demo_runtime_lease_recovery_v123_recovery_seq_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_demo_runtime_lease_recovery_mutation_v123() FROM PUBLIC;

COMMIT;
