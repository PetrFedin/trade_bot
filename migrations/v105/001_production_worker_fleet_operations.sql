BEGIN;

CREATE SCHEMA IF NOT EXISTS astra_v105;

CREATE OR REPLACE FUNCTION astra_v105.reject_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'append-only relation % cannot be mutated', TG_TABLE_NAME;
END;
$$;

CREATE TABLE IF NOT EXISTS astra_v105.enrollment_replay_guard (
    token_id text PRIMARY KEY,
    nonce text NOT NULL UNIQUE,
    consumed_at timestamptz NOT NULL,
    CHECK (length(token_id) BETWEEN 1 AND 128),
    CHECK (length(nonce) BETWEEN 1 AND 128)
);

CREATE TABLE IF NOT EXISTS astra_v105.fleet_worker (
    worker_id text PRIMARY KEY,
    deployment_id text NOT NULL,
    zone text NOT NULL,
    certificate_fingerprint text NOT NULL,
    identity_generation bigint NOT NULL CHECK (identity_generation > 0),
    state text NOT NULL CHECK (state IN ('ACTIVE', 'DRAINING', 'STOPPED', 'QUARANTINED', 'REVOKED')),
    heartbeat_sequence bigint NOT NULL DEFAULT 0 CHECK (heartbeat_sequence >= 0),
    last_heartbeat_at timestamptz,
    active_claims integer NOT NULL DEFAULT 0 CHECK (active_claims >= 0),
    recovery_required boolean NOT NULL DEFAULT false,
    observed_at timestamptz NOT NULL,
    CHECK (certificate_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS astra_v105.worker_identity_rotation (
    rotation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    worker_id text NOT NULL REFERENCES astra_v105.fleet_worker(worker_id),
    previous_certificate_fingerprint text NOT NULL,
    new_certificate_fingerprint text NOT NULL,
    identity_generation bigint NOT NULL CHECK (identity_generation > 1),
    operator_a text NOT NULL,
    operator_b text NOT NULL,
    rotated_at timestamptz NOT NULL,
    CHECK (operator_a <> operator_b)
);

CREATE TABLE IF NOT EXISTS astra_v105.worker_heartbeat_event (
    heartbeat_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    worker_id text NOT NULL REFERENCES astra_v105.fleet_worker(worker_id),
    identity_generation bigint NOT NULL CHECK (identity_generation > 0),
    heartbeat_sequence bigint NOT NULL CHECK (heartbeat_sequence > 0),
    observed_at timestamptz NOT NULL,
    UNIQUE(worker_id, identity_generation, heartbeat_sequence)
);

CREATE TABLE IF NOT EXISTS astra_v105.fleet_task (
    task_id text PRIMARY KEY,
    task_type text NOT NULL CHECK (task_type IN ('ENROLL', 'ROTATE_IDENTITY', 'DRAIN', 'CONTAIN', 'RELEASE', 'EVIDENCE_UPLOAD')),
    priority integer NOT NULL DEFAULT 0,
    state text NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING', 'CLAIMED', 'COMPLETED', 'FAILED', 'QUARANTINED')),
    owner_id text,
    not_before timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    claimed_at timestamptz,
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS fleet_task_due_idx
    ON astra_v105.fleet_task(state, not_before, priority DESC, created_at, task_id);

CREATE TABLE IF NOT EXISTS astra_v105.fleet_containment (
    containment_id text PRIMARY KEY,
    epoch bigint NOT NULL UNIQUE CHECK (epoch > 0),
    scope text NOT NULL CHECK (scope IN ('FLEET', 'ZONE', 'DEPLOYMENT', 'WORKER')),
    target text NOT NULL,
    reason text NOT NULL,
    activated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_v105.fleet_containment_release (
    release_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    containment_id text NOT NULL REFERENCES astra_v105.fleet_containment(containment_id),
    epoch bigint NOT NULL CHECK (epoch > 0),
    evidence_digest text NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{64}$'),
    operator_a text NOT NULL,
    operator_b text NOT NULL,
    released_at timestamptz NOT NULL,
    CHECK (operator_a <> operator_b),
    UNIQUE(containment_id, epoch)
);

CREATE TABLE IF NOT EXISTS astra_v105.autoscale_decision (
    decision_id text PRIMARY KEY,
    digest text NOT NULL UNIQUE CHECK (digest ~ '^[0-9a-f]{64}$'),
    current_replicas integer NOT NULL CHECK (current_replicas >= 0),
    desired_replicas integer NOT NULL CHECK (desired_replicas >= 1),
    reason text NOT NULL,
    observed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_v105.evidence_object (
    object_key text PRIMARY KEY,
    object_sha256 text NOT NULL CHECK (object_sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    upload_id text NOT NULL,
    recorded_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_v105.fleet_event (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL,
    worker_id text,
    occurred_at timestamptz NOT NULL,
    details jsonb NOT NULL,
    previous_digest text NOT NULL CHECK (previous_digest ~ '^[0-9a-f]{64}$'),
    digest text NOT NULL UNIQUE CHECK (digest ~ '^[0-9a-f]{64}$')
);

CREATE OR REPLACE FUNCTION astra_v105.claim_fleet_task(p_owner_id text, p_now timestamptz)
RETURNS TABLE(task_id text, task_type text, generation bigint, fencing_token bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, astra_v105
AS $$
BEGIN
    RETURN QUERY
    WITH candidate AS (
        SELECT task.task_id
          FROM astra_v105.fleet_task AS task
         WHERE task.state = 'PENDING' AND task.not_before <= p_now
         ORDER BY task.priority DESC, task.created_at, task.task_id
         FOR UPDATE SKIP LOCKED
         LIMIT 1
    )
    UPDATE astra_v105.fleet_task AS task
       SET state = 'CLAIMED', owner_id = p_owner_id, claimed_at = p_now,
           generation = task.generation + 1, fencing_token = task.fencing_token + 1
      FROM candidate
     WHERE task.task_id = candidate.task_id
    RETURNING task.task_id, task.task_type, task.generation, task.fencing_token;
END;
$$;

CREATE OR REPLACE FUNCTION astra_v105.record_fleet_heartbeat(
    p_worker_id text,
    p_identity_generation bigint,
    p_sequence bigint,
    p_observed_at timestamptz
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, astra_v105
AS $$
BEGIN
    UPDATE astra_v105.fleet_worker
       SET heartbeat_sequence = p_sequence,
           last_heartbeat_at = p_observed_at,
           observed_at = p_observed_at
     WHERE worker_id = p_worker_id
       AND identity_generation = p_identity_generation
       AND heartbeat_sequence < p_sequence;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'heartbeat fencing rejected';
    END IF;

    INSERT INTO astra_v105.worker_heartbeat_event(worker_id, identity_generation, heartbeat_sequence, observed_at)
    VALUES (p_worker_id, p_identity_generation, p_sequence, p_observed_at);
END;
$$;

DO $$
DECLARE
    relation_name text;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'enrollment_replay_guard', 'worker_identity_rotation', 'worker_heartbeat_event',
        'fleet_containment', 'fleet_containment_release', 'autoscale_decision',
        'evidence_object', 'fleet_event'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I_append_only ON astra_v105.%I', relation_name, relation_name);
        EXECUTE format(
            'CREATE TRIGGER %I_append_only BEFORE UPDATE OR DELETE ON astra_v105.%I FOR EACH ROW EXECUTE FUNCTION astra_v105.reject_append_only_mutation()',
            relation_name, relation_name
        );
    END LOOP;
END;
$$;

REVOKE ALL ON SCHEMA astra_v105 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA astra_v105 FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA astra_v105 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA astra_v105 FROM PUBLIC;

COMMIT;
