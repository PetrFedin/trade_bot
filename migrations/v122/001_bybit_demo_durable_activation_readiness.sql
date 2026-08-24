BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_demo_activation_readiness_v122 (
    readiness_seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    readiness_id text NOT NULL UNIQUE CHECK (readiness_id ~ '^[0-9a-f]{64}$'),
    git_sha text NOT NULL CHECK (git_sha ~ '^[0-9a-f]{40}$'),
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    canonical_manifest text NOT NULL CHECK (btrim(canonical_manifest) <> ''),
    postgres_evidence_sha256 text NOT NULL CHECK (postgres_evidence_sha256 ~ '^[0-9a-f]{64}$'),
    connected_preflight_evidence_sha256 text NOT NULL CHECK (connected_preflight_evidence_sha256 ~ '^[0-9a-f]{64}$'),
    trading_credential_evidence_sha256 text NOT NULL CHECK (trading_credential_evidence_sha256 ~ '^[0-9a-f]{64}$'),
    control_status_evidence_sha256 text NOT NULL CHECK (control_status_evidence_sha256 ~ '^[0-9a-f]{64}$'),
    recorded_by text NOT NULL CHECK (btrim(recorded_by) <> '' AND length(recorded_by) <= 128),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status = 'READY_FOR_EXPLICIT_ACTIVATION_GATES'),
    operator_action_required boolean NOT NULL DEFAULT true CHECK (operator_action_required = true),
    arm_performed boolean NOT NULL DEFAULT false CHECK (arm_performed = false),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    order_write_performed boolean NOT NULL DEFAULT false CHECK (order_write_performed = false),
    order_writes_supported boolean NOT NULL DEFAULT false CHECK (order_writes_supported = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    immutable_record boolean NOT NULL DEFAULT true CHECK (immutable_record = true),
    CHECK (expires_at > created_at),
    CHECK (expires_at <= created_at + interval '5 minutes')
);

CREATE INDEX IF NOT EXISTS astra_bybit_demo_readiness_latest_idx_v122
    ON astra_bybit_demo_activation_readiness_v122(git_sha, readiness_seq DESC);

CREATE TABLE IF NOT EXISTS astra_bybit_demo_activation_readiness_claim_v122 (
    claim_seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id text NOT NULL UNIQUE CHECK (claim_id ~ '^[0-9a-f]{64}$'),
    readiness_id text NOT NULL UNIQUE REFERENCES astra_bybit_demo_activation_readiness_v122(readiness_id),
    git_sha text NOT NULL CHECK (git_sha ~ '^[0-9a-f]{40}$'),
    operator_id text NOT NULL CHECK (btrim(operator_id) <> '' AND length(operator_id) <= 128),
    purpose text NOT NULL CHECK (purpose = 'ARM_NEW_ENTRIES'),
    claimed_at timestamptz NOT NULL,
    immutable_record boolean NOT NULL DEFAULT true CHECK (immutable_record = true),
    order_write_performed boolean NOT NULL DEFAULT false CHECK (order_write_performed = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false)
);

CREATE INDEX IF NOT EXISTS astra_bybit_demo_readiness_claim_git_idx_v122
    ON astra_bybit_demo_activation_readiness_claim_v122(git_sha, claim_seq DESC);

CREATE TABLE IF NOT EXISTS astra_bybit_demo_activation_readiness_arm_link_v122 (
    link_seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id text NOT NULL UNIQUE REFERENCES astra_bybit_demo_activation_readiness_claim_v122(claim_id),
    control_event_id text NOT NULL UNIQUE REFERENCES astra_bybit_demo_control_event_v121(event_id),
    linked_at timestamptz NOT NULL,
    immutable_record boolean NOT NULL DEFAULT true CHECK (immutable_record = true),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false)
);

CREATE OR REPLACE FUNCTION astra_reject_bybit_demo_readiness_mutation_v122()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit Demo activation readiness v122 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_demo_readiness_append_only_v122
    ON astra_bybit_demo_activation_readiness_v122;
CREATE TRIGGER astra_bybit_demo_readiness_append_only_v122
BEFORE UPDATE OR DELETE ON astra_bybit_demo_activation_readiness_v122
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_demo_readiness_mutation_v122();

DROP TRIGGER IF EXISTS astra_bybit_demo_readiness_claim_append_only_v122
    ON astra_bybit_demo_activation_readiness_claim_v122;
CREATE TRIGGER astra_bybit_demo_readiness_claim_append_only_v122
BEFORE UPDATE OR DELETE ON astra_bybit_demo_activation_readiness_claim_v122
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_demo_readiness_mutation_v122();

DROP TRIGGER IF EXISTS astra_bybit_demo_readiness_arm_link_append_only_v122
    ON astra_bybit_demo_activation_readiness_arm_link_v122;
CREATE TRIGGER astra_bybit_demo_readiness_arm_link_append_only_v122
BEFORE UPDATE OR DELETE ON astra_bybit_demo_activation_readiness_arm_link_v122
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_demo_readiness_mutation_v122();

REVOKE ALL ON astra_bybit_demo_activation_readiness_v122 FROM PUBLIC;
REVOKE ALL ON astra_bybit_demo_activation_readiness_claim_v122 FROM PUBLIC;
REVOKE ALL ON astra_bybit_demo_activation_readiness_arm_link_v122 FROM PUBLIC;
REVOKE ALL ON SEQUENCE astra_bybit_demo_activation_readiness_v122_readiness_seq_seq FROM PUBLIC;
REVOKE ALL ON SEQUENCE astra_bybit_demo_activation_readiness_claim_v122_claim_seq_seq FROM PUBLIC;
REVOKE ALL ON SEQUENCE astra_bybit_demo_activation_readiness_arm_link_v122_link_seq_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_demo_readiness_mutation_v122() FROM PUBLIC;

COMMIT;
