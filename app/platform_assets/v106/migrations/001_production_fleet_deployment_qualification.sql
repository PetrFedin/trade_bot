BEGIN;

CREATE SCHEMA IF NOT EXISTS astra_v106;

CREATE OR REPLACE FUNCTION astra_v106.reject_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'append-only relation % cannot be mutated', TG_TABLE_NAME;
END;
$$;

CREATE TABLE IF NOT EXISTS astra_v106.manifest_replay_guard (
    manifest_id text PRIMARY KEY,
    nonce text NOT NULL UNIQUE,
    consumed_at timestamptz NOT NULL,
    CHECK (length(manifest_id) BETWEEN 1 AND 128),
    CHECK (length(nonce) BETWEEN 1 AND 128)
);

CREATE TABLE IF NOT EXISTS astra_v106.deployment_manifest (
    manifest_id text PRIMARY KEY,
    rollout_id text NOT NULL,
    deployment_id text NOT NULL,
    fleet_id text NOT NULL,
    environment text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    image_digest text NOT NULL CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
    config_digest text NOT NULL CHECK (config_digest ~ '^sha256:[0-9a-f]{64}$'),
    replicas integer NOT NULL CHECK (replicas > 0),
    canary_replicas integer NOT NULL CHECK (canary_replicas > 0 AND canary_replicas <= replicas),
    issued_at timestamptz NOT NULL,
    not_before timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    nonce text NOT NULL UNIQUE,
    key_id text NOT NULL,
    signature text NOT NULL CHECK (signature ~ '^[0-9a-f]{64}$'),
    manifest_digest text NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (issued_at <= not_before AND not_before < expires_at)
);

CREATE TABLE IF NOT EXISTS astra_v106.deployment_qualification (
    qualification_id text PRIMARY KEY,
    manifest_id text NOT NULL REFERENCES astra_v106.deployment_manifest(manifest_id),
    policy_digest text NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    manifest_digest text NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    generation bigint NOT NULL CHECK (generation > 0),
    state text NOT NULL CHECK (state IN (
        'PLANNED','PREFLIGHT','CANARY','OBSERVING','PROMOTABLE',
        'PROMOTION_PENDING','PROMOTED','ROLLBACK_PENDING','ROLLED_BACK',
        'COMPLETED','BLOCKED','QUARANTINED'
    )),
    failure_samples integer NOT NULL DEFAULT 0 CHECK (failure_samples >= 0),
    fencing_token bigint NOT NULL DEFAULT 1 CHECK (fencing_token > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS astra_v106.kubernetes_snapshot (
    snapshot_id text PRIMARY KEY,
    qualification_id text NOT NULL REFERENCES astra_v106.deployment_qualification(qualification_id),
    observed_at timestamptz NOT NULL,
    cluster text NOT NULL,
    namespace text NOT NULL,
    service_account text NOT NULL,
    deployment_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    desired_replicas integer NOT NULL CHECK (desired_replicas >= 0),
    available_replicas integer NOT NULL CHECK (available_replicas >= 0),
    canary_ready_replicas integer NOT NULL CHECK (canary_ready_replicas >= 0),
    snapshot_digest text NOT NULL CHECK (snapshot_digest ~ '^[0-9a-f]{64}$'),
    snapshot_json jsonb NOT NULL,
    CHECK (available_replicas <= desired_replicas),
    UNIQUE (qualification_id, observed_at, snapshot_digest)
);

CREATE TABLE IF NOT EXISTS astra_v106.preflight_gate_set (
    qualification_id text PRIMARY KEY REFERENCES astra_v106.deployment_qualification(qualification_id),
    evaluated_at timestamptz NOT NULL,
    gate_digest text NOT NULL CHECK (gate_digest ~ '^[0-9a-f]{64}$'),
    passed boolean NOT NULL,
    critical_failure_count integer NOT NULL CHECK (critical_failure_count >= 0),
    evidence_json jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_v106.observation_sample (
    qualification_id text NOT NULL REFERENCES astra_v106.deployment_qualification(qualification_id),
    sample_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    sample_digest text NOT NULL CHECK (sample_digest ~ '^[0-9a-f]{64}$'),
    gate_digest text NOT NULL CHECK (gate_digest ~ '^[0-9a-f]{64}$'),
    passed boolean NOT NULL,
    PRIMARY KEY (qualification_id, sample_id),
    UNIQUE (qualification_id, observed_at),
    UNIQUE (qualification_id, sample_digest)
);

CREATE TABLE IF NOT EXISTS astra_v106.qualification_event (
    qualification_id text NOT NULL REFERENCES astra_v106.deployment_qualification(qualification_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL,
    observed_at timestamptz NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    previous_digest text NOT NULL CHECK (previous_digest ~ '^[0-9a-f]{64}$'),
    event_digest text NOT NULL CHECK (event_digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (qualification_id, sequence),
    UNIQUE (qualification_id, event_digest)
);

CREATE TABLE IF NOT EXISTS astra_v106.rollout_action_outbox (
    action_id text PRIMARY KEY,
    qualification_id text NOT NULL REFERENCES astra_v106.deployment_qualification(qualification_id),
    action_type text NOT NULL CHECK (action_type IN ('PROMOTE','ROLLBACK')),
    generation bigint NOT NULL CHECK (generation > 0),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    idempotency_key text NOT NULL UNIQUE,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    signature text NOT NULL CHECK (signature ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('PENDING','CLAIMED','ACKED','FAILED')),
    attempt_count integer NOT NULL CHECK (attempt_count BETWEEN 0 AND 1),
    claimed_by text,
    claimed_at timestamptz,
    receipt_digest text CHECK (receipt_digest IS NULL OR receipt_digest ~ '^[0-9a-f]{64}$'),
    acknowledged_at timestamptz,
    created_at timestamptz NOT NULL,
    CHECK ((status = 'PENDING' AND attempt_count = 0) OR (status <> 'PENDING' AND attempt_count = 1)),
    CHECK ((status IN ('ACKED','FAILED')) = (receipt_digest IS NOT NULL AND acknowledged_at IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS rollout_action_pending_idx
ON astra_v106.rollout_action_outbox (created_at, action_id)
WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS astra_v106.certificate_drill (
    drill_id text PRIMARY KEY,
    worker_id text NOT NULL,
    old_identity_generation bigint NOT NULL CHECK (old_identity_generation > 0),
    new_identity_generation bigint CHECK (new_identity_generation IS NULL OR new_identity_generation = old_identity_generation + 1),
    old_fingerprint text NOT NULL CHECK (old_fingerprint ~ '^[0-9a-f]{64}$'),
    new_fingerprint text CHECK (new_fingerprint IS NULL OR new_fingerprint ~ '^[0-9a-f]{64}$'),
    approver_a text NOT NULL,
    approver_b text NOT NULL,
    state text NOT NULL CHECK (state IN ('PLANNED','ISSUED','ACTIVATED','OLD_REVOKED','VERIFIED','FAILED')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (approver_a <> approver_b),
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS astra_v106.certificate_drill_event (
    drill_id text NOT NULL REFERENCES astra_v106.certificate_drill(drill_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    state text NOT NULL,
    worker_id text NOT NULL,
    identity_generation bigint NOT NULL CHECK (identity_generation > 0),
    evidence_digest text NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (drill_id, sequence)
);

CREATE TABLE IF NOT EXISTS astra_v106.backup_manifest (
    backup_id text PRIMARY KEY,
    source_environment text NOT NULL,
    created_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    object_digest text NOT NULL CHECK (object_digest ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    postgres_lsn text NOT NULL,
    schema_version text NOT NULL,
    encrypted boolean NOT NULL CHECK (encrypted),
    kms_key_id text NOT NULL,
    integrity_digest text NOT NULL CHECK (integrity_digest ~ '^[0-9a-f]{64}$'),
    CHECK (created_at <= completed_at)
);

CREATE TABLE IF NOT EXISTS astra_v106.disaster_recovery_drill (
    drill_id text PRIMARY KEY,
    backup_id text NOT NULL REFERENCES astra_v106.backup_manifest(backup_id),
    target_environment text NOT NULL,
    state text NOT NULL CHECK (state IN ('PLANNED','RESTORING','VERIFYING','PASSED','FAILED','QUARANTINED')),
    max_rpo_seconds integer NOT NULL CHECK (max_rpo_seconds > 0),
    max_rto_seconds integer NOT NULL CHECK (max_rto_seconds > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (target_environment LIKE 'drill-%'),
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS astra_v106.disaster_recovery_event (
    drill_id text NOT NULL REFERENCES astra_v106.disaster_recovery_drill(drill_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    state text NOT NULL,
    backup_id text NOT NULL,
    evidence_json jsonb NOT NULL,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (drill_id, sequence)
);

CREATE OR REPLACE FUNCTION astra_v106.claim_rollout_action(
    p_worker_id text,
    p_generation bigint,
    p_fencing_token bigint,
    p_claimed_at timestamptz
)
RETURNS SETOF astra_v106.rollout_action_outbox
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = astra_v106, pg_temp
AS $$
DECLARE
    v_action_id text;
BEGIN
    SELECT action_id INTO v_action_id
    FROM astra_v106.rollout_action_outbox
    WHERE status = 'PENDING'
      AND generation = p_generation
      AND fencing_token = p_fencing_token
    ORDER BY created_at, action_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1;

    IF v_action_id IS NULL THEN
        RETURN;
    END IF;

    UPDATE astra_v106.rollout_action_outbox
    SET status = 'CLAIMED',
        claimed_by = p_worker_id,
        claimed_at = p_claimed_at,
        attempt_count = attempt_count + 1
    WHERE action_id = v_action_id
      AND status = 'PENDING'
      AND generation = p_generation
      AND fencing_token = p_fencing_token
      AND attempt_count = 0;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'rollout action fencing race';
    END IF;

    RETURN QUERY
    SELECT * FROM astra_v106.rollout_action_outbox WHERE action_id = v_action_id;
END;
$$;

DROP TRIGGER IF EXISTS manifest_replay_guard_append_only ON astra_v106.manifest_replay_guard;
CREATE TRIGGER manifest_replay_guard_append_only
BEFORE UPDATE OR DELETE ON astra_v106.manifest_replay_guard
FOR EACH ROW EXECUTE FUNCTION astra_v106.reject_append_only_mutation();

DROP TRIGGER IF EXISTS deployment_manifest_append_only ON astra_v106.deployment_manifest;
CREATE TRIGGER deployment_manifest_append_only
BEFORE UPDATE OR DELETE ON astra_v106.deployment_manifest
FOR EACH ROW EXECUTE FUNCTION astra_v106.reject_append_only_mutation();

DROP TRIGGER IF EXISTS kubernetes_snapshot_append_only ON astra_v106.kubernetes_snapshot;
CREATE TRIGGER kubernetes_snapshot_append_only
BEFORE UPDATE OR DELETE ON astra_v106.kubernetes_snapshot
FOR EACH ROW EXECUTE FUNCTION astra_v106.reject_append_only_mutation();

DROP TRIGGER IF EXISTS preflight_gate_set_append_only ON astra_v106.preflight_gate_set;
CREATE TRIGGER preflight_gate_set_append_only
BEFORE UPDATE OR DELETE ON astra_v106.preflight_gate_set
FOR EACH ROW EXECUTE FUNCTION astra_v106.reject_append_only_mutation();

DROP TRIGGER IF EXISTS observation_sample_append_only ON astra_v106.observation_sample;
CREATE TRIGGER observation_sample_append_only
BEFORE UPDATE OR DELETE ON astra_v106.observation_sample
FOR EACH ROW EXECUTE FUNCTION astra_v106.reject_append_only_mutation();

DROP TRIGGER IF EXISTS qualification_event_append_only ON astra_v106.qualification_event;
CREATE TRIGGER qualification_event_append_only
BEFORE UPDATE OR DELETE ON astra_v106.qualification_event
FOR EACH ROW EXECUTE FUNCTION astra_v106.reject_append_only_mutation();

DROP TRIGGER IF EXISTS certificate_drill_event_append_only ON astra_v106.certificate_drill_event;
CREATE TRIGGER certificate_drill_event_append_only
BEFORE UPDATE OR DELETE ON astra_v106.certificate_drill_event
FOR EACH ROW EXECUTE FUNCTION astra_v106.reject_append_only_mutation();

DROP TRIGGER IF EXISTS backup_manifest_append_only ON astra_v106.backup_manifest;
CREATE TRIGGER backup_manifest_append_only
BEFORE UPDATE OR DELETE ON astra_v106.backup_manifest
FOR EACH ROW EXECUTE FUNCTION astra_v106.reject_append_only_mutation();

DROP TRIGGER IF EXISTS disaster_recovery_event_append_only ON astra_v106.disaster_recovery_event;
CREATE TRIGGER disaster_recovery_event_append_only
BEFORE UPDATE OR DELETE ON astra_v106.disaster_recovery_event
FOR EACH ROW EXECUTE FUNCTION astra_v106.reject_append_only_mutation();

REVOKE ALL ON SCHEMA astra_v106 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA astra_v106 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA astra_v106 FROM PUBLIC;

COMMIT;
