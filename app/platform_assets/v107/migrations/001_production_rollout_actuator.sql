BEGIN;

CREATE TABLE IF NOT EXISTS astra_rollout_replay_v107 (
    command_id text PRIMARY KEY,
    nonce text NOT NULL UNIQUE,
    idempotency_key text NOT NULL UNIQUE,
    consumed_at timestamptz NOT NULL
);


CREATE TABLE IF NOT EXISTS astra_rollout_fence_v107 (
    deployment_uid text PRIMARY KEY,
    deployment_uid text NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    command_id text NOT NULL UNIQUE,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_rollout_execution_v107 (
    command_id text PRIMARY KEY,
    action_id text NOT NULL UNIQUE,
    command_digest text NOT NULL UNIQUE CHECK (command_digest ~ '^[0-9a-f]{64}$'),
    command_json jsonb NOT NULL,
    state text NOT NULL CHECK (state IN (
        'PENDING', 'CLAIMED', 'PREFLIGHT', 'MUTATION_STARTED',
        'VERIFYING', 'SUCCEEDED', 'FAILED', 'UNCERTAIN', 'QUARANTINED'
    )),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    target_replicas integer NOT NULL CHECK (target_replicas >= 0),
    claimed_by text,
    claimed_at timestamptz,
    recovery_by text,
    recovery_claimed_at timestamptz,
    preflight_digest text CHECK (preflight_digest IS NULL OR preflight_digest ~ '^[0-9a-f]{64}$'),
    pre_snapshot_digest text CHECK (pre_snapshot_digest IS NULL OR pre_snapshot_digest ~ '^[0-9a-f]{64}$'),
    mutation_attempts smallint NOT NULL DEFAULT 0 CHECK (mutation_attempts IN (0, 1)),
    patch_digest text CHECK (patch_digest IS NULL OR patch_digest ~ '^[0-9a-f]{64}$'),
    mutation_started_at timestamptz,
    failure_reason text CHECK (failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 512),
    receipt_digest text CHECK (receipt_digest IS NULL OR receipt_digest ~ '^[0-9a-f]{64}$'),
    receipt_json jsonb,
    completed_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK ((claimed_by IS NULL) = (claimed_at IS NULL)),
    CHECK ((recovery_by IS NULL) = (recovery_claimed_at IS NULL)),
    CHECK (
        (mutation_attempts = 0 AND patch_digest IS NULL AND mutation_started_at IS NULL)
        OR
        (mutation_attempts = 1 AND patch_digest IS NOT NULL AND mutation_started_at IS NOT NULL)
    ),
    CHECK (
        state <> 'MUTATION_STARTED'
        OR mutation_attempts = 1
    ),
    CHECK (
        (receipt_digest IS NULL AND receipt_json IS NULL AND completed_at IS NULL)
        OR
        (receipt_digest IS NOT NULL AND receipt_json IS NOT NULL AND completed_at IS NOT NULL)
    ),
    CHECK (updated_at >= created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS astra_rollout_active_fence_v107
    ON astra_rollout_execution_v107 (deployment_uid, fencing_token);

CREATE INDEX IF NOT EXISTS astra_rollout_pending_v107
    ON astra_rollout_execution_v107 (created_at, command_id)
    WHERE state = 'PENDING';

CREATE INDEX IF NOT EXISTS astra_rollout_recovery_v107
    ON astra_rollout_execution_v107 (updated_at, command_id)
    WHERE state IN ('MUTATION_STARTED', 'VERIFYING', 'UNCERTAIN');

CREATE TABLE IF NOT EXISTS astra_rollout_outbox_v107 (
    event_id text PRIMARY KEY,
    command_id text NOT NULL REFERENCES astra_rollout_execution_v107(command_id),
    event_type text NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    published_at timestamptz
);

CREATE TABLE IF NOT EXISTS astra_rollout_event_v107 (
    event_sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    command_id text NOT NULL REFERENCES astra_rollout_execution_v107(command_id),
    event_type text NOT NULL,
    observed_at timestamptz NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$')
);

CREATE OR REPLACE FUNCTION astra_rollout_event_append_only_v107()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_rollout_event_v107 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_rollout_event_append_only_v107 ON astra_rollout_event_v107;
CREATE TRIGGER astra_rollout_event_append_only_v107
BEFORE UPDATE OR DELETE ON astra_rollout_event_v107
FOR EACH ROW EXECUTE FUNCTION astra_rollout_event_append_only_v107();

REVOKE ALL ON astra_rollout_replay_v107 FROM PUBLIC;
REVOKE ALL ON astra_rollout_fence_v107 FROM PUBLIC;
REVOKE ALL ON astra_rollout_execution_v107 FROM PUBLIC;
REVOKE ALL ON astra_rollout_outbox_v107 FROM PUBLIC;
REVOKE ALL ON astra_rollout_event_v107 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_rollout_event_append_only_v107() FROM PUBLIC;

COMMIT;
