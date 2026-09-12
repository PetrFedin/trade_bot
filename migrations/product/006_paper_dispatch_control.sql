CREATE TABLE IF NOT EXISTS astra_paper_dispatch_control_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    mode TEXT NOT NULL CHECK (mode IN ('HALTED', 'ARMED')),
    version BIGINT NOT NULL CHECK (version > 0),
    operator_id TEXT NOT NULL CHECK (length(btrim(operator_id)) > 0),
    reason TEXT NOT NULL CHECK (length(btrim(reason)) > 0),
    armed_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (mode = 'HALTED' AND armed_until IS NULL)
        OR (mode = 'ARMED' AND armed_until IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS astra_paper_dispatch_control_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (
        event_type IN ('ARM', 'HALT', 'DISPATCH_AUTHORIZED')
    ),
    intent_id TEXT,
    control_version BIGINT NOT NULL CHECK (control_version >= 0),
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_astra_paper_dispatch_control_events_time
ON astra_paper_dispatch_control_events(occurred_at, sequence);

CREATE OR REPLACE FUNCTION astra_reject_paper_dispatch_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ASTRA_PAPER_DISPATCH_EVENT_APPEND_ONLY';
END;
$$;

DROP TRIGGER IF EXISTS astra_paper_dispatch_control_events_append_only
ON astra_paper_dispatch_control_events;
CREATE TRIGGER astra_paper_dispatch_control_events_append_only
BEFORE UPDATE OR DELETE ON astra_paper_dispatch_control_events
FOR EACH ROW EXECUTE FUNCTION astra_reject_paper_dispatch_event_mutation();

CREATE OR REPLACE FUNCTION astra_reject_paper_dispatch_event_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ASTRA_PAPER_DISPATCH_EVENT_TRUNCATE_FORBIDDEN';
END;
$$;

DROP TRIGGER IF EXISTS astra_paper_dispatch_control_events_no_truncate
ON astra_paper_dispatch_control_events;
CREATE TRIGGER astra_paper_dispatch_control_events_no_truncate
BEFORE TRUNCATE ON astra_paper_dispatch_control_events
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_paper_dispatch_event_truncate();
