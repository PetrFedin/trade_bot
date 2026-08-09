BEGIN;

CREATE SCHEMA IF NOT EXISTS astra_v102;

CREATE TABLE IF NOT EXISTS astra_v102.soak_campaign (
    campaign_id text PRIMARY KEY,
    generation bigint NOT NULL CHECK (generation > 0),
    state text NOT NULL CHECK (state IN ('CREATED','ACTIVE','RUNNING','COMPLETED','BLOCKED','QUARANTINED')),
    plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    maximum_runs integer NOT NULL CHECK (maximum_runs > 0),
    minimum_verified_runs integer NOT NULL CHECK (minimum_verified_runs BETWEEN 1 AND maximum_runs),
    completed_runs integer NOT NULL DEFAULT 0 CHECK (completed_runs BETWEEN 0 AND maximum_runs),
    verified_runs integer NOT NULL DEFAULT 0 CHECK (verified_runs BETWEEN 0 AND completed_runs),
    total_failures integer NOT NULL DEFAULT 0 CHECK (total_failures >= 0),
    consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    tail_digest text NOT NULL CHECK (tail_digest ~ '^[0-9a-f]{64}$'),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (ends_at > starts_at)
);

CREATE TABLE IF NOT EXISTS astra_v102.soak_campaign_event (
    campaign_id text NOT NULL REFERENCES astra_v102.soak_campaign(campaign_id),
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL,
    from_state text NOT NULL,
    to_state text NOT NULL,
    occurred_at timestamptz NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    previous_digest text NOT NULL CHECK (previous_digest ~ '^[0-9a-f]{64}$'),
    event_digest text NOT NULL CHECK (event_digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (campaign_id, sequence),
    UNIQUE (campaign_id, event_digest),
    CHECK (COALESCE((attributes->>'external_order_routing_allowed')::boolean, false) = false),
    CHECK (COALESCE((attributes->>'live_trading_allowed')::boolean, false) = false)
);

CREATE TABLE IF NOT EXISTS astra_v102.soak_run_evidence (
    campaign_id text NOT NULL REFERENCES astra_v102.soak_campaign(campaign_id),
    run_id text NOT NULL,
    run_index integer NOT NULL CHECK (run_index > 0),
    generation bigint NOT NULL CHECK (generation > 0),
    qualification_id text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('VERIFIED_CLEAN','PREFLIGHT_BLOCKED','RECOVERY_REQUIRED','RESIDUAL_PAPER_EXPOSURE','QUARANTINED','MISSED_WINDOW')),
    captured_at timestamptz NOT NULL,
    cleanup_verified boolean NOT NULL,
    kill_switch_engaged boolean NOT NULL,
    evidence_digest text NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{64}$'),
    artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    retain_until timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (campaign_id, run_id),
    UNIQUE (campaign_id, run_index),
    CHECK (retain_until > captured_at)
);

CREATE TABLE IF NOT EXISTS astra_v102.soak_lease (
    campaign_id text PRIMARY KEY REFERENCES astra_v102.soak_campaign(campaign_id),
    owner_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    acquired_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    released boolean NOT NULL DEFAULT false,
    lease_digest text NOT NULL CHECK (lease_digest ~ '^[0-9a-f]{64}$'),
    CHECK (released OR expires_at > acquired_at)
);

CREATE OR REPLACE FUNCTION astra_v102.reject_append_only_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only table cannot be updated or deleted';
END;
$$;

DROP TRIGGER IF EXISTS soak_campaign_event_append_only ON astra_v102.soak_campaign_event;
CREATE TRIGGER soak_campaign_event_append_only
BEFORE UPDATE OR DELETE ON astra_v102.soak_campaign_event
FOR EACH ROW EXECUTE FUNCTION astra_v102.reject_append_only_change();

DROP TRIGGER IF EXISTS soak_run_evidence_append_only ON astra_v102.soak_run_evidence;
CREATE TRIGGER soak_run_evidence_append_only
BEFORE UPDATE OR DELETE ON astra_v102.soak_run_evidence
FOR EACH ROW EXECUTE FUNCTION astra_v102.reject_append_only_change();

REVOKE ALL ON SCHEMA astra_v102 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA astra_v102 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA astra_v102 FROM PUBLIC;

COMMIT;
