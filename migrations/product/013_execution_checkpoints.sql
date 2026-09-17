BEGIN;

CREATE TABLE IF NOT EXISTS astra_execution_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_execution_checkpoint_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    checkpoint_id TEXT NOT NULL
        REFERENCES astra_execution_checkpoints(checkpoint_id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state = 'RESOLVED'),
    cumulative_quantity TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_astra_execution_checkpoint_events_checkpoint
    ON astra_execution_checkpoint_events(checkpoint_id, sequence DESC);

CREATE OR REPLACE FUNCTION astra_reject_execution_checkpoint_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ASTRA_EXECUTION_CHECKPOINT_APPEND_ONLY';
END;
$$;

DROP TRIGGER IF EXISTS astra_execution_checkpoints_append_only
    ON astra_execution_checkpoints;
CREATE TRIGGER astra_execution_checkpoints_append_only
BEFORE UPDATE OR DELETE ON astra_execution_checkpoints
FOR EACH ROW EXECUTE FUNCTION astra_reject_execution_checkpoint_mutation();

DROP TRIGGER IF EXISTS astra_execution_checkpoint_events_append_only
    ON astra_execution_checkpoint_events;
CREATE TRIGGER astra_execution_checkpoint_events_append_only
BEFORE UPDATE OR DELETE ON astra_execution_checkpoint_events
FOR EACH ROW EXECUTE FUNCTION astra_reject_execution_checkpoint_mutation();

CREATE OR REPLACE FUNCTION astra_reject_execution_checkpoint_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ASTRA_EXECUTION_CHECKPOINT_TRUNCATE_FORBIDDEN';
END;
$$;

DROP TRIGGER IF EXISTS astra_execution_checkpoints_no_truncate
    ON astra_execution_checkpoints;
CREATE TRIGGER astra_execution_checkpoints_no_truncate
BEFORE TRUNCATE ON astra_execution_checkpoints
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_execution_checkpoint_truncate();

DROP TRIGGER IF EXISTS astra_execution_checkpoint_events_no_truncate
    ON astra_execution_checkpoint_events;
CREATE TRIGGER astra_execution_checkpoint_events_no_truncate
BEFORE TRUNCATE ON astra_execution_checkpoint_events
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_execution_checkpoint_truncate();

COMMIT;
