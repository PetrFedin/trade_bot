BEGIN;

CREATE SCHEMA IF NOT EXISTS astra_v104;

CREATE TABLE IF NOT EXISTS astra_v104.worker_claim (
    claim_id text PRIMARY KEY,
    campaign_id text NOT NULL,
    run_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    state text NOT NULL CHECK (state IN ('READY','CLAIMED','RUNNING','SPOOLING','UPLOADING','COMPLETED','RECOVERY_REQUIRED','QUARANTINED')),
    signed_claim_json jsonb NOT NULL,
    signed_claim_digest text NOT NULL CHECK (signed_claim_digest ~ '^[0-9a-f]{64}$'),
    worker_id text,
    deployment_id text,
    not_before timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    claimed_at timestamptz,
    heartbeat_at timestamptz,
    heartbeat_sequence bigint NOT NULL DEFAULT 0 CHECK (heartbeat_sequence >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (expires_at > not_before)
);

CREATE UNIQUE INDEX IF NOT EXISTS worker_claim_one_active_run
ON astra_v104.worker_claim (run_id)
WHERE state IN ('CLAIMED','RUNNING','SPOOLING','UPLOADING');

CREATE INDEX IF NOT EXISTS worker_claim_due_idx
ON astra_v104.worker_claim (not_before, claim_id)
WHERE state = 'READY';

-- Canonical work claiming query uses FOR UPDATE SKIP LOCKED.
CREATE OR REPLACE FUNCTION astra_v104.claim_next_worker_job(
    p_worker_id text,
    p_deployment_id text,
    p_now timestamptz
) RETURNS SETOF astra_v104.worker_claim
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = astra_v104, pg_temp
AS $$
BEGIN
    RETURN QUERY
    WITH candidate AS (
        SELECT claim_id
        FROM astra_v104.worker_claim
        WHERE state = 'READY'
          AND not_before <= p_now
          AND expires_at > p_now
        ORDER BY not_before, claim_id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    UPDATE astra_v104.worker_claim AS claim
       SET state = 'CLAIMED',
           worker_id = p_worker_id,
           deployment_id = p_deployment_id,
           claimed_at = p_now,
           updated_at = p_now
      FROM candidate
     WHERE claim.claim_id = candidate.claim_id
    RETURNING claim.*;
END;
$$;

CREATE TABLE IF NOT EXISTS astra_v104.worker_attestation (
    worker_id text NOT NULL,
    deployment_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    image_digest text NOT NULL,
    source_commit text NOT NULL,
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    nonce text NOT NULL,
    key_id text NOT NULL,
    signature text NOT NULL CHECK (signature ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (worker_id, deployment_id, generation),
    UNIQUE (nonce),
    CHECK (expires_at > created_at)
);

CREATE TABLE IF NOT EXISTS astra_v104.worker_event (
    claim_id text NOT NULL REFERENCES astra_v104.worker_claim(claim_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL,
    state text NOT NULL,
    occurred_at timestamptz NOT NULL,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    previous_digest text NOT NULL CHECK (previous_digest ~ '^[0-9a-f]{64}$'),
    event_digest text NOT NULL CHECK (event_digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (claim_id, sequence),
    UNIQUE (event_digest)
);

CREATE TABLE IF NOT EXISTS astra_v104.evidence_spool (
    record_id text PRIMARY KEY,
    claim_id text NOT NULL REFERENCES astra_v104.worker_claim(claim_id),
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    byte_length bigint NOT NULL CHECK (byte_length >= 0),
    state text NOT NULL CHECK (state IN ('PENDING','UPLOADING','ACKNOWLEDGED','QUARANTINED')),
    retention_until timestamptz NOT NULL,
    legal_hold boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    acknowledged_at timestamptz
);

CREATE TABLE IF NOT EXISTS astra_v104.multipart_upload (
    record_id text PRIMARY KEY REFERENCES astra_v104.evidence_spool(record_id),
    upload_id text NOT NULL UNIQUE,
    object_key text NOT NULL UNIQUE,
    completed_object_digest text CHECK (completed_object_digest IS NULL OR completed_object_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS astra_v104.multipart_upload_part (
    record_id text NOT NULL REFERENCES astra_v104.multipart_upload(record_id),
    part_number integer NOT NULL CHECK (part_number > 0),
    part_digest text NOT NULL CHECK (part_digest ~ '^[0-9a-f]{64}$'),
    etag text NOT NULL,
    byte_length integer NOT NULL CHECK (byte_length > 0),
    uploaded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (record_id, part_number)
);

CREATE TABLE IF NOT EXISTS astra_v104.worker_dead_letter (
    record_id text PRIMARY KEY,
    claim_id text NOT NULL REFERENCES astra_v104.worker_claim(claim_id),
    reason text NOT NULL,
    detail text NOT NULL,
    attempt integer NOT NULL CHECK (attempt >= 0),
    occurred_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_v104.worker_dead_letter_release (
    record_id text NOT NULL REFERENCES astra_v104.worker_dead_letter(record_id),
    release_sequence bigint NOT NULL CHECK (release_sequence > 0),
    released_by text NOT NULL,
    released_at timestamptz NOT NULL,
    reason text NOT NULL,
    previous_digest text NOT NULL CHECK (previous_digest ~ '^[0-9a-f]{64}$'),
    release_digest text NOT NULL CHECK (release_digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (record_id, release_sequence),
    UNIQUE (release_digest)
);

CREATE OR REPLACE FUNCTION astra_v104.reject_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only relation % rejects %', TG_TABLE_NAME, TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS worker_event_append_only ON astra_v104.worker_event;
CREATE TRIGGER worker_event_append_only
BEFORE UPDATE OR DELETE ON astra_v104.worker_event
FOR EACH ROW EXECUTE FUNCTION astra_v104.reject_mutation();

DROP TRIGGER IF EXISTS worker_attestation_append_only ON astra_v104.worker_attestation;
CREATE TRIGGER worker_attestation_append_only
BEFORE UPDATE OR DELETE ON astra_v104.worker_attestation
FOR EACH ROW EXECUTE FUNCTION astra_v104.reject_mutation();

DROP TRIGGER IF EXISTS multipart_upload_part_append_only ON astra_v104.multipart_upload_part;
CREATE TRIGGER multipart_upload_part_append_only
BEFORE UPDATE OR DELETE ON astra_v104.multipart_upload_part
FOR EACH ROW EXECUTE FUNCTION astra_v104.reject_mutation();

DROP TRIGGER IF EXISTS worker_dead_letter_append_only ON astra_v104.worker_dead_letter;
CREATE TRIGGER worker_dead_letter_append_only
BEFORE UPDATE OR DELETE ON astra_v104.worker_dead_letter
FOR EACH ROW EXECUTE FUNCTION astra_v104.reject_mutation();

DROP TRIGGER IF EXISTS worker_dead_letter_release_append_only ON astra_v104.worker_dead_letter_release;
CREATE TRIGGER worker_dead_letter_release_append_only
BEFORE UPDATE OR DELETE ON astra_v104.worker_dead_letter_release
FOR EACH ROW EXECUTE FUNCTION astra_v104.reject_mutation();

REVOKE ALL ON SCHEMA astra_v104 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA astra_v104 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA astra_v104 FROM PUBLIC;

COMMIT;
