BEGIN;

CREATE TABLE IF NOT EXISTS astra_operational_decision_leases (
    ticket_id TEXT PRIMARY KEY
        REFERENCES astra_operational_decision_tickets(ticket_id),
    owner_id TEXT NOT NULL,
    release_identity TEXT NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    acquired_at TIMESTAMPTZ NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (lease_expires_at >= acquired_at),
    CHECK (updated_at >= acquired_at)
);

CREATE INDEX IF NOT EXISTS idx_operational_decision_leases_expiry
ON astra_operational_decision_leases(lease_expires_at, ticket_id);

CREATE TABLE IF NOT EXISTS astra_operational_decision_lease_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL
        REFERENCES astra_operational_decision_tickets(ticket_id),
    event_type TEXT NOT NULL CHECK (
        event_type IN ('CLAIM', 'RECLAIM', 'RENEW', 'RELEASE', 'COMPLETE')
    ),
    owner_id TEXT NOT NULL,
    release_identity TEXT NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    lease_expires_at TIMESTAMPTZ,
    outcome_id TEXT,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operational_decision_lease_events_ticket
ON astra_operational_decision_lease_events(ticket_id, sequence);

CREATE TABLE IF NOT EXISTS astra_operational_decision_safety_evidence (
    evidence_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL
        REFERENCES astra_operational_decision_tickets(ticket_id),
    owner_id TEXT NOT NULL,
    release_identity TEXT NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    checkpoint_id TEXT
        REFERENCES astra_operational_market_continuity(checkpoint_id),
    first_bar_id TEXT,
    last_bar_id TEXT,
    continuity_reasons JSONB NOT NULL,
    readiness_reasons JSONB NOT NULL,
    control_mode TEXT NOT NULL CHECK (control_mode IN ('HALTED', 'ARMED')),
    control_version BIGINT NOT NULL CHECK (control_version >= 0),
    observed_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (first_bar_id IS NULL AND last_bar_id IS NULL)
        OR (first_bar_id IS NOT NULL AND last_bar_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_operational_decision_safety_ticket
ON astra_operational_decision_safety_evidence(ticket_id, observed_at, evidence_id);

CREATE OR REPLACE FUNCTION reject_astra_operational_decision_lease_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'astra_operational_decision_lease_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_operational_decision_lease_events_no_update
ON astra_operational_decision_lease_events;
CREATE TRIGGER astra_operational_decision_lease_events_no_update
BEFORE UPDATE ON astra_operational_decision_lease_events
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_decision_lease_event_mutation();

DROP TRIGGER IF EXISTS astra_operational_decision_lease_events_no_delete
ON astra_operational_decision_lease_events;
CREATE TRIGGER astra_operational_decision_lease_events_no_delete
BEFORE DELETE ON astra_operational_decision_lease_events
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_decision_lease_event_mutation();

CREATE OR REPLACE FUNCTION reject_astra_operational_decision_safety_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'astra_operational_decision_safety_evidence is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_operational_decision_safety_no_update
ON astra_operational_decision_safety_evidence;
CREATE TRIGGER astra_operational_decision_safety_no_update
BEFORE UPDATE ON astra_operational_decision_safety_evidence
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_decision_safety_mutation();

DROP TRIGGER IF EXISTS astra_operational_decision_safety_no_delete
ON astra_operational_decision_safety_evidence;
CREATE TRIGGER astra_operational_decision_safety_no_delete
BEFORE DELETE ON astra_operational_decision_safety_evidence
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_decision_safety_mutation();

COMMIT;
