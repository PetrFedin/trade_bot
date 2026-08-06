BEGIN;

CREATE TABLE IF NOT EXISTS astra_signing_keyring_v108 (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    generation bigint NOT NULL UNIQUE CHECK (generation > 0),
    snapshot_digest text NOT NULL UNIQUE CHECK (snapshot_digest ~ '^[0-9a-f]{64}$'),
    snapshot_json jsonb NOT NULL,
    root_key_id text NOT NULL,
    root_signature_b64 text NOT NULL,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (issued_at < expires_at),
    CHECK (updated_at >= issued_at)
);

CREATE TABLE IF NOT EXISTS astra_signature_replay_v108 (
    signature_id text PRIMARY KEY,
    nonce text NOT NULL UNIQUE,
    purpose text NOT NULL CHECK (purpose IN (
        'RELEASE_APPROVAL', 'RISK_APPROVAL', 'CONTROLLER_COMMAND', 'EXECUTOR_RECEIPT'
    )),
    domain text NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    key_id text NOT NULL,
    key_generation bigint NOT NULL CHECK (key_generation > 0),
    keyring_generation bigint NOT NULL CHECK (keyring_generation > 0),
    consumed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_rollout_authorization_v108 (
    bundle_id text PRIMARY KEY,
    command_digest text NOT NULL UNIQUE CHECK (command_digest ~ '^[0-9a-f]{64}$'),
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    predecessor_release_identity_digest text NOT NULL CHECK (
        predecessor_release_identity_digest ~ '^[0-9a-f]{64}$'
    ),
    authorization_digest text NOT NULL UNIQUE CHECK (authorization_digest ~ '^[0-9a-f]{64}$'),
    bundle_digest text NOT NULL UNIQUE CHECK (bundle_digest ~ '^[0-9a-f]{64}$'),
    bundle_json jsonb NOT NULL,
    keyring_generation bigint NOT NULL CHECK (keyring_generation > 0),
    created_at timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS astra_rollout_authorization_bundle_command_v108
    ON astra_rollout_authorization_v108 (bundle_digest, command_digest);

CREATE TABLE IF NOT EXISTS astra_receipt_authorization_v108 (
    receipt_id text PRIMARY KEY,
    receipt_digest text NOT NULL UNIQUE CHECK (receipt_digest ~ '^[0-9a-f]{64}$'),
    command_digest text NOT NULL CHECK (command_digest ~ '^[0-9a-f]{64}$'),
    authorization_bundle_digest text NOT NULL CHECK (authorization_bundle_digest ~ '^[0-9a-f]{64}$'),
    payload_digest text NOT NULL UNIQUE CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    authorization_digest text NOT NULL UNIQUE CHECK (authorization_digest ~ '^[0-9a-f]{64}$'),
    executor_signature_id text NOT NULL UNIQUE
        REFERENCES astra_signature_replay_v108(signature_id),
    receipt_json jsonb NOT NULL,
    keyring_generation bigint NOT NULL CHECK (keyring_generation > 0),
    created_at timestamptz NOT NULL,
    FOREIGN KEY (authorization_bundle_digest, command_digest)
        REFERENCES astra_rollout_authorization_v108(bundle_digest, command_digest)
);

CREATE TABLE IF NOT EXISTS astra_signing_event_v108 (
    event_sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL,
    subject_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$')
);

CREATE OR REPLACE FUNCTION astra_signing_event_append_only_v108()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_signing_event_v108 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_signing_event_append_only_v108 ON astra_signing_event_v108;
CREATE TRIGGER astra_signing_event_append_only_v108
BEFORE UPDATE OR DELETE ON astra_signing_event_v108
FOR EACH ROW EXECUTE FUNCTION astra_signing_event_append_only_v108();

REVOKE ALL ON astra_signing_keyring_v108 FROM PUBLIC;
REVOKE ALL ON astra_signature_replay_v108 FROM PUBLIC;
REVOKE ALL ON astra_rollout_authorization_v108 FROM PUBLIC;
REVOKE ALL ON astra_receipt_authorization_v108 FROM PUBLIC;
REVOKE ALL ON astra_signing_event_v108 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_signing_event_append_only_v108() FROM PUBLIC;

COMMIT;
