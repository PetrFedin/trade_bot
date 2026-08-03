BEGIN;

CREATE SCHEMA IF NOT EXISTS astra_v103;

CREATE TABLE IF NOT EXISTS astra_v103.control_plane_campaign (
    campaign_id text PRIMARY KEY,
    generation bigint NOT NULL CHECK (generation > 0),
    state text NOT NULL CHECK (state IN ('CREATED','READY','LEASED','PROBING','UPLOADING','BLOCKED','QUARANTINED','RETIRED')),
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    next_due_at timestamptz NOT NULL,
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    event_tail_digest text NOT NULL DEFAULT repeat('0', 64) CHECK (event_tail_digest ~ '^[0-9a-f]{64}$'),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (ends_at > starts_at)
);

CREATE TABLE IF NOT EXISTS astra_v103.campaign_lease (
    campaign_id text PRIMARY KEY REFERENCES astra_v103.control_plane_campaign(campaign_id),
    owner_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    acquired_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    CHECK (expires_at > acquired_at)
);

CREATE TABLE IF NOT EXISTS astra_v103.worker_heartbeat (
    campaign_id text NOT NULL REFERENCES astra_v103.control_plane_campaign(campaign_id),
    generation bigint NOT NULL CHECK (generation > 0),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    owner_id text NOT NULL,
    deployment_id text NOT NULL,
    build_identity text NOT NULL,
    observed_at timestamptz NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    PRIMARY KEY (campaign_id, generation, fencing_token, observed_at)
);

CREATE TABLE IF NOT EXISTS astra_v103.control_plane_event (
    campaign_id text NOT NULL REFERENCES astra_v103.control_plane_campaign(campaign_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    fencing_token bigint NOT NULL CHECK (fencing_token >= 0),
    occurred_at timestamptz NOT NULL,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    previous_digest text NOT NULL CHECK (previous_digest ~ '^[0-9a-f]{64}$'),
    event_digest text NOT NULL CHECK (event_digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (campaign_id, sequence),
    UNIQUE (campaign_id, event_digest)
);

CREATE TABLE IF NOT EXISTS astra_v103.read_only_probe_run (
    run_id text PRIMARY KEY,
    request_id text NOT NULL UNIQUE,
    campaign_id text NOT NULL REFERENCES astra_v103.control_plane_campaign(campaign_id),
    generation bigint NOT NULL CHECK (generation > 0),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    account_id text NOT NULL,
    host text NOT NULL,
    method text NOT NULL CHECK (method IN ('GET','HEAD')),
    path text NOT NULL,
    probe_digest text NOT NULL CHECK (probe_digest ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('STARTED','VERIFIED','FAILED','ERROR','BLOCKED')),
    created_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    completed_at timestamptz,
    mutation_count integer NOT NULL DEFAULT 0 CHECK (mutation_count = 0),
    external_order_routing_attempted boolean NOT NULL DEFAULT false CHECK (external_order_routing_attempted = false),
    CHECK (deadline_at > created_at)
);

CREATE TABLE IF NOT EXISTS astra_v103.evidence_upload (
    upload_id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES astra_v103.read_only_probe_run(run_id),
    campaign_id text NOT NULL REFERENCES astra_v103.control_plane_campaign(campaign_id),
    generation bigint NOT NULL CHECK (generation > 0),
    total_size bigint NOT NULL CHECK (total_size > 0),
    chunk_size integer NOT NULL CHECK (chunk_size >= 256),
    expected_digest text NOT NULL CHECK (expected_digest ~ '^[0-9a-f]{64}$'),
    next_offset bigint NOT NULL DEFAULT 0 CHECK (next_offset >= 0 AND next_offset <= total_size),
    state text NOT NULL CHECK (state IN ('IN_PROGRESS','COMPLETE','CORRUPT')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    retention_until timestamptz NOT NULL,
    legal_hold boolean NOT NULL DEFAULT false,
    previous_manifest_digest text NOT NULL CHECK (previous_manifest_digest ~ '^[0-9a-f]{64}$'),
    manifest_digest text NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS astra_v103.evidence_chunk (
    upload_id text NOT NULL REFERENCES astra_v103.evidence_upload(upload_id),
    chunk_offset bigint NOT NULL CHECK (chunk_offset >= 0),
    chunk_size integer NOT NULL CHECK (chunk_size > 0),
    chunk_digest text NOT NULL CHECK (chunk_digest ~ '^[0-9a-f]{64}$'),
    object_key text NOT NULL,
    accepted_at timestamptz NOT NULL,
    PRIMARY KEY (upload_id, chunk_offset)
);

CREATE TABLE IF NOT EXISTS astra_v103.incident (
    incident_id text PRIMARY KEY,
    dedupe_key text NOT NULL,
    campaign_id text NOT NULL REFERENCES astra_v103.control_plane_campaign(campaign_id),
    run_id text,
    severity text NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
    status text NOT NULL CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
    code text NOT NULL,
    details_digest text NOT NULL CHECK (details_digest ~ '^[0-9a-f]{64}$'),
    opened_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    acknowledged_by text,
    resolved_by text
);

CREATE UNIQUE INDEX IF NOT EXISTS incident_open_dedupe
ON astra_v103.incident(dedupe_key)
WHERE status <> 'RESOLVED';

CREATE TABLE IF NOT EXISTS astra_v103.retention_tombstone (
    upload_id text PRIMARY KEY REFERENCES astra_v103.evidence_upload(upload_id),
    manifest_digest text NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    deleted_size bigint NOT NULL CHECK (deleted_size >= 0),
    deleted_at timestamptz NOT NULL,
    deletion_reason text NOT NULL
);

CREATE OR REPLACE FUNCTION astra_v103.deny_append_only_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only relation % cannot be updated or deleted', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS control_plane_event_append_only ON astra_v103.control_plane_event;
CREATE TRIGGER control_plane_event_append_only
BEFORE UPDATE OR DELETE ON astra_v103.control_plane_event
FOR EACH ROW EXECUTE FUNCTION astra_v103.deny_append_only_mutation();

DROP TRIGGER IF EXISTS evidence_chunk_append_only ON astra_v103.evidence_chunk;
CREATE TRIGGER evidence_chunk_append_only
BEFORE UPDATE OR DELETE ON astra_v103.evidence_chunk
FOR EACH ROW EXECUTE FUNCTION astra_v103.deny_append_only_mutation();

DROP TRIGGER IF EXISTS worker_heartbeat_append_only ON astra_v103.worker_heartbeat;
CREATE TRIGGER worker_heartbeat_append_only
BEFORE UPDATE OR DELETE ON astra_v103.worker_heartbeat
FOR EACH ROW EXECUTE FUNCTION astra_v103.deny_append_only_mutation();

CREATE OR REPLACE FUNCTION astra_v103.claim_campaign_lease(
    p_campaign_id text,
    p_owner_id text,
    p_generation bigint,
    p_now timestamptz,
    p_lease_ttl interval
)
RETURNS TABLE (
    campaign_id text,
    owner_id text,
    generation bigint,
    fencing_token bigint,
    acquired_at timestamptz,
    expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = astra_v103, pg_temp
AS $$
DECLARE
    v_campaign astra_v103.control_plane_campaign%ROWTYPE;
    v_token bigint;
BEGIN
    IF p_owner_id IS NULL OR btrim(p_owner_id) = '' OR p_lease_ttl <= interval '0 seconds' THEN
        RETURN;
    END IF;

    SELECT * INTO v_campaign
    FROM astra_v103.control_plane_campaign c
    WHERE c.campaign_id = p_campaign_id
    FOR UPDATE;

    IF NOT FOUND
       OR v_campaign.generation <> p_generation
       OR v_campaign.state <> 'READY'
       OR v_campaign.next_due_at > p_now
       OR v_campaign.ends_at < p_now
       OR EXISTS (
            SELECT 1 FROM astra_v103.incident i
            WHERE i.campaign_id = p_campaign_id
              AND i.status <> 'RESOLVED'
              AND i.severity = 'CRITICAL'
       ) THEN
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1 FROM astra_v103.campaign_lease l
        WHERE l.campaign_id = p_campaign_id
          AND l.expires_at > p_now
    ) THEN
        RETURN;
    END IF;

    v_token := v_campaign.fencing_token + 1;

    INSERT INTO astra_v103.campaign_lease(
        campaign_id, owner_id, generation, fencing_token, acquired_at, expires_at
    ) VALUES (
        p_campaign_id, p_owner_id, p_generation, v_token, p_now, p_now + p_lease_ttl
    )
    ON CONFLICT (campaign_id) DO UPDATE SET
        owner_id = EXCLUDED.owner_id,
        generation = EXCLUDED.generation,
        fencing_token = EXCLUDED.fencing_token,
        acquired_at = EXCLUDED.acquired_at,
        expires_at = EXCLUDED.expires_at;

    UPDATE astra_v103.control_plane_campaign
    SET state = 'LEASED', fencing_token = v_token, version = version + 1, updated_at = p_now
    WHERE control_plane_campaign.campaign_id = p_campaign_id;

    RETURN QUERY SELECT p_campaign_id, p_owner_id, p_generation, v_token, p_now, p_now + p_lease_ttl;
END;
$$;

CREATE OR REPLACE FUNCTION astra_v103.record_worker_heartbeat(
    p_campaign_id text,
    p_owner_id text,
    p_generation bigint,
    p_fencing_token bigint,
    p_deployment_id text,
    p_build_identity text,
    p_observed_at timestamptz,
    p_lease_ttl interval
)
RETURNS TABLE(result_code text, expires_at timestamptz)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = astra_v103, pg_temp
AS $$
DECLARE
    v_lease astra_v103.campaign_lease%ROWTYPE;
BEGIN
    SELECT * INTO v_lease
    FROM astra_v103.campaign_lease l
    WHERE l.campaign_id = p_campaign_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 'LEASE_MISSING'::text, NULL::timestamptz;
        RETURN;
    END IF;
    IF v_lease.generation <> p_generation THEN
        RETURN QUERY SELECT 'STALE_GENERATION'::text, NULL::timestamptz;
        RETURN;
    END IF;
    IF v_lease.fencing_token <> p_fencing_token OR v_lease.owner_id <> p_owner_id THEN
        RETURN QUERY SELECT 'STALE_FENCING_TOKEN'::text, NULL::timestamptz;
        RETURN;
    END IF;
    IF v_lease.expires_at <= p_observed_at THEN
        RETURN QUERY SELECT 'LEASE_EXPIRED'::text, v_lease.expires_at;
        RETURN;
    END IF;

    INSERT INTO astra_v103.worker_heartbeat(
        campaign_id, generation, fencing_token, owner_id, deployment_id,
        build_identity, observed_at, lease_expires_at
    ) VALUES (
        p_campaign_id, p_generation, p_fencing_token, p_owner_id, p_deployment_id,
        p_build_identity, p_observed_at, p_observed_at + p_lease_ttl
    );

    UPDATE astra_v103.campaign_lease
    SET expires_at = p_observed_at + p_lease_ttl
    WHERE campaign_lease.campaign_id = p_campaign_id;

    RETURN QUERY SELECT 'OK'::text, p_observed_at + p_lease_ttl;
END;
$$;

CREATE OR REPLACE FUNCTION astra_v103.append_control_plane_event(
    p_campaign_id text,
    p_event_type text,
    p_generation bigint,
    p_fencing_token bigint,
    p_occurred_at timestamptz,
    p_attributes jsonb,
    p_previous_digest text,
    p_event_digest text
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = astra_v103, pg_temp
AS $$
DECLARE
    v_sequence bigint;
    v_tail text;
BEGIN
    SELECT event_tail_digest INTO v_tail
    FROM astra_v103.control_plane_campaign
    WHERE campaign_id = p_campaign_id
    FOR UPDATE;

    IF NOT FOUND OR v_tail <> p_previous_digest THEN
        RAISE EXCEPTION 'event tail mismatch';
    END IF;

    SELECT COALESCE(MAX(sequence), 0) + 1 INTO v_sequence
    FROM astra_v103.control_plane_event
    WHERE campaign_id = p_campaign_id;

    INSERT INTO astra_v103.control_plane_event(
        campaign_id, sequence, event_type, generation, fencing_token,
        occurred_at, attributes, previous_digest, event_digest
    ) VALUES (
        p_campaign_id, v_sequence, p_event_type, p_generation, p_fencing_token,
        p_occurred_at, p_attributes, p_previous_digest, p_event_digest
    );

    UPDATE astra_v103.control_plane_campaign
    SET event_tail_digest = p_event_digest, version = version + 1, updated_at = p_occurred_at
    WHERE campaign_id = p_campaign_id;

    RETURN v_sequence;
END;
$$;

REVOKE ALL ON SCHEMA astra_v103 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA astra_v103 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA astra_v103 FROM PUBLIC;

COMMIT;
