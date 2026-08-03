BEGIN;
CREATE SCHEMA IF NOT EXISTS astra_v99;

CREATE TABLE IF NOT EXISTS astra_v99.paper_roundtrip_events (
    sequence_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_trip_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    state text NOT NULL,
    occurred_at timestamptz NOT NULL,
    attributes jsonb NOT NULL,
    previous_digest char(64) NOT NULL,
    digest char(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS paper_roundtrip_events_identity_idx
    ON astra_v99.paper_roundtrip_events (round_trip_id, generation, sequence_id);

CREATE TABLE IF NOT EXISTS astra_v99.deployment_checkpoints (
    service_name text PRIMARY KEY,
    state text NOT NULL,
    instance_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 0),
    updated_at timestamptz NOT NULL,
    lease_expires_at timestamptz,
    drain_deadline timestamptz,
    crash_times timestamptz[] NOT NULL DEFAULT '{}',
    quarantine_reason text NOT NULL DEFAULT '',
    digest char(64) NOT NULL
);

REVOKE ALL ON SCHEMA astra_v99 FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA astra_v99 FROM PUBLIC;
COMMIT;
