CREATE TABLE IF NOT EXISTS astra_execution_facts (
    execution_fact_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_execution_fact_sources (
    source_execution_id TEXT PRIMARY KEY,
    execution_fact_id TEXT NOT NULL REFERENCES astra_execution_facts(execution_fact_id),
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_execution_projection_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    execution_fact_id TEXT NOT NULL REFERENCES astra_execution_facts(execution_fact_id),
    state TEXT NOT NULL CHECK (state IN ('QUARANTINED', 'PROJECTED')),
    reason TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    CHECK ((state = 'QUARANTINED' AND reason IS NOT NULL AND length(btrim(reason)) > 0)
        OR (state = 'PROJECTED' AND reason IS NULL))
);

CREATE OR REPLACE FUNCTION astra_validate_execution_projection_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1 FROM astra_execution_facts
    WHERE execution_fact_id = NEW.execution_fact_id
    FOR UPDATE;

    IF NEW.state <> 'PROJECTED' AND EXISTS (
        SELECT 1 FROM astra_execution_projection_events
        WHERE execution_fact_id = NEW.execution_fact_id
          AND state = 'PROJECTED'
    ) THEN
        RAISE EXCEPTION 'ASTRA_EXECUTION_PROJECTION_STATE_REGRESSION';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS astra_execution_projection_transition_guard
ON astra_execution_projection_events;
CREATE TRIGGER astra_execution_projection_transition_guard
BEFORE INSERT ON astra_execution_projection_events
FOR EACH ROW EXECUTE FUNCTION astra_validate_execution_projection_transition();

CREATE OR REPLACE FUNCTION astra_reject_execution_fact_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ASTRA_EXECUTION_FACT_APPEND_ONLY';
END;
$$;

DROP TRIGGER IF EXISTS astra_execution_facts_append_only ON astra_execution_facts;
CREATE TRIGGER astra_execution_facts_append_only
BEFORE UPDATE OR DELETE ON astra_execution_facts
FOR EACH ROW EXECUTE FUNCTION astra_reject_execution_fact_mutation();

DROP TRIGGER IF EXISTS astra_execution_fact_sources_append_only ON astra_execution_fact_sources;
CREATE TRIGGER astra_execution_fact_sources_append_only
BEFORE UPDATE OR DELETE ON astra_execution_fact_sources
FOR EACH ROW EXECUTE FUNCTION astra_reject_execution_fact_mutation();

DROP TRIGGER IF EXISTS astra_execution_projection_events_append_only ON astra_execution_projection_events;
CREATE TRIGGER astra_execution_projection_events_append_only
BEFORE UPDATE OR DELETE ON astra_execution_projection_events
FOR EACH ROW EXECUTE FUNCTION astra_reject_execution_fact_mutation();

CREATE OR REPLACE FUNCTION astra_reject_execution_fact_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ASTRA_EXECUTION_FACT_TRUNCATE_FORBIDDEN';
END;
$$;

DROP TRIGGER IF EXISTS astra_execution_facts_no_truncate ON astra_execution_facts;
CREATE TRIGGER astra_execution_facts_no_truncate
BEFORE TRUNCATE ON astra_execution_facts
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_execution_fact_truncate();

DROP TRIGGER IF EXISTS astra_execution_fact_sources_no_truncate ON astra_execution_fact_sources;
CREATE TRIGGER astra_execution_fact_sources_no_truncate
BEFORE TRUNCATE ON astra_execution_fact_sources
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_execution_fact_truncate();

DROP TRIGGER IF EXISTS astra_execution_projection_events_no_truncate ON astra_execution_projection_events;
CREATE TRIGGER astra_execution_projection_events_no_truncate
BEFORE TRUNCATE ON astra_execution_projection_events
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_execution_fact_truncate();

CREATE INDEX IF NOT EXISTS idx_astra_execution_projection_fact_sequence
ON astra_execution_projection_events(execution_fact_id, sequence DESC);
