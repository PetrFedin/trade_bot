BEGIN;

CREATE TABLE IF NOT EXISTS astra_remote_sign_policy_v109 (
    provider_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    snapshot_digest text NOT NULL CHECK (snapshot_digest ~ '^[0-9a-f]{64}$'),
    snapshot_json jsonb NOT NULL,
    endpoint_origin text NOT NULL CHECK (endpoint_origin ~ '^https://'),
    mtls_identity_ref text NOT NULL,
    signing_key_id text NOT NULL,
    attestation_key_id text NOT NULL,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    installed_at timestamptz NOT NULL,
    PRIMARY KEY (provider_id, generation),
    UNIQUE (snapshot_digest),
    CHECK (issued_at < expires_at)
);

CREATE TABLE IF NOT EXISTS astra_remote_sign_request_v109 (
    request_id text PRIMARY KEY,
    nonce text NOT NULL UNIQUE,
    provider_id text NOT NULL,
    policy_generation bigint NOT NULL,
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    request_digest text NOT NULL UNIQUE CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    request_json jsonb NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('CREATED', 'DISPATCH_STARTED', 'SIGNED', 'REJECTED', 'UNCERTAIN', 'QUARANTINED')),
    dispatch_worker_id text,
    dispatch_started_at timestamptz,
    signature_b64 text,
    attestation_json jsonb,
    failure_reason text,
    created_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT astra_remote_sign_request_policy_fk_v109
        FOREIGN KEY (provider_id, policy_generation)
        REFERENCES astra_remote_sign_policy_v109(provider_id, generation),
    CHECK (created_at < deadline_at),
    CHECK ((state <> 'DISPATCH_STARTED') OR dispatch_started_at IS NOT NULL),
    CHECK ((state <> 'SIGNED') OR (signature_b64 IS NOT NULL AND attestation_json IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS astra_remote_sign_outbox_v109 (
    request_id text PRIMARY KEY REFERENCES astra_remote_sign_request_v109(request_id) ON DELETE RESTRICT,
    payload_b64 text NOT NULL,
    created_at timestamptz NOT NULL,
    dispatched_at timestamptz
);

CREATE TABLE IF NOT EXISTS astra_remote_sign_checkpoint_v109 (
    provider_id text PRIMARY KEY,
    policy_generation bigint NOT NULL CHECK (policy_generation > 0),
    audit_sequence bigint NOT NULL CHECK (audit_sequence > 0),
    hardware_signing_counter bigint NOT NULL CHECK (hardware_signing_counter > 0),
    audit_chain_root text NOT NULL CHECK (audit_chain_root ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_remote_sign_event_v109 (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id text REFERENCES astra_remote_sign_request_v109(request_id) ON DELETE RESTRICT,
    event_type text NOT NULL,
    observed_at timestamptz NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    details_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE OR REPLACE FUNCTION astra_reject_remote_sign_event_mutation_v109()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_remote_sign_event_v109 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_remote_sign_event_append_only_v109 ON astra_remote_sign_event_v109;
CREATE TRIGGER astra_remote_sign_event_append_only_v109
BEFORE UPDATE OR DELETE ON astra_remote_sign_event_v109
FOR EACH ROW EXECUTE FUNCTION astra_reject_remote_sign_event_mutation_v109();

CREATE INDEX IF NOT EXISTS astra_remote_sign_request_state_idx_v109
    ON astra_remote_sign_request_v109(state, updated_at);
CREATE INDEX IF NOT EXISTS astra_remote_sign_event_request_idx_v109
    ON astra_remote_sign_event_v109(request_id, event_id);

REVOKE ALL ON astra_remote_sign_policy_v109 FROM PUBLIC;
REVOKE ALL ON astra_remote_sign_request_v109 FROM PUBLIC;
REVOKE ALL ON astra_remote_sign_outbox_v109 FROM PUBLIC;
REVOKE ALL ON astra_remote_sign_checkpoint_v109 FROM PUBLIC;
REVOKE ALL ON astra_remote_sign_event_v109 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_remote_sign_event_mutation_v109() FROM PUBLIC;

COMMIT;
