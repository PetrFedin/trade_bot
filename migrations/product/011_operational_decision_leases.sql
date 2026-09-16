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
    first_bar_id TEXT
        REFERENCES astra_operational_market_bars(bar_id),
    last_bar_id TEXT
        REFERENCES astra_operational_market_bars(bar_id),
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

CREATE OR REPLACE FUNCTION astra_lock_validate_operational_decision_safety_v1(
    p_ticket_id TEXT,
    p_owner_id TEXT,
    p_release_identity TEXT,
    p_fencing_token BIGINT
)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    evidence_row RECORD;
    first_bar RECORD;
    checkpoint_row RECORD;
    window_bar RECORD;
    previous_close TIMESTAMPTZ := NULL;
    first_seen_id TEXT := NULL;
    last_seen_id TEXT := NULL;
    observed_count BIGINT := 0;
    expected_count BIGINT;
    delta_seconds NUMERIC;
BEGIN
    SELECT
        e.checkpoint_id,
        e.first_bar_id,
        e.last_bar_id,
        e.continuity_reasons,
        e.readiness_reasons,
        t.bar_id AS ticket_bar_id,
        b.provider,
        b.venue,
        b.symbol,
        b.interval_seconds,
        b.close_time AS ticket_close_time
    INTO evidence_row
    FROM astra_operational_decision_safety_evidence e
    JOIN astra_operational_decision_tickets t ON t.ticket_id=e.ticket_id
    JOIN astra_operational_market_bars b ON b.bar_id=t.bar_id
    WHERE e.ticket_id=p_ticket_id
      AND e.owner_id=p_owner_id
      AND e.release_identity=p_release_identity
      AND e.fencing_token=p_fencing_token
    ORDER BY e.observed_at DESC, e.evidence_id DESC
    LIMIT 1;

    IF NOT FOUND
       OR evidence_row.checkpoint_id IS NULL
       OR evidence_row.first_bar_id IS NULL
       OR evidence_row.last_bar_id IS NULL
       OR evidence_row.last_bar_id <> evidence_row.ticket_bar_id
       OR jsonb_array_length(evidence_row.continuity_reasons) <> 0
       OR jsonb_array_length(evidence_row.readiness_reasons) <> 0 THEN
        RAISE EXCEPTION 'DECISION_SAFETY_EVIDENCE_INVALIDATED';
    END IF;

    SELECT provider, venue, symbol, interval_seconds, open_time, close_time
    INTO first_bar
    FROM astra_operational_market_bars
    WHERE bar_id=evidence_row.first_bar_id;

    IF NOT FOUND
       OR first_bar.provider <> evidence_row.provider
       OR first_bar.venue <> evidence_row.venue
       OR first_bar.symbol <> evidence_row.symbol
       OR first_bar.interval_seconds <> evidence_row.interval_seconds
       OR first_bar.close_time > evidence_row.ticket_close_time THEN
        RAISE EXCEPTION 'DECISION_SAFETY_EVIDENCE_INVALIDATED';
    END IF;

    delta_seconds := EXTRACT(
        EPOCH FROM (evidence_row.ticket_close_time - first_bar.close_time)
    );
    IF delta_seconds < 0
       OR MOD(delta_seconds, evidence_row.interval_seconds) <> 0 THEN
        RAISE EXCEPTION 'DECISION_SAFETY_EVIDENCE_INVALIDATED';
    END IF;
    expected_count := (delta_seconds / evidence_row.interval_seconds)::BIGINT + 1;

    FOR window_bar IN
        SELECT b.bar_id, b.open_time, b.close_time
        FROM astra_operational_market_bars b
        WHERE b.provider=evidence_row.provider
          AND b.venue=evidence_row.venue
          AND b.symbol=evidence_row.symbol
          AND b.interval_seconds=evidence_row.interval_seconds
          AND b.close_time>=first_bar.close_time
          AND b.close_time<=evidence_row.ticket_close_time
        ORDER BY b.close_time, b.bar_id
        FOR UPDATE
    LOOP
        observed_count := observed_count + 1;
        IF first_seen_id IS NULL THEN
            first_seen_id := window_bar.bar_id;
        END IF;
        last_seen_id := window_bar.bar_id;

        IF previous_close IS NOT NULL AND previous_close <> window_bar.open_time THEN
            RAISE EXCEPTION 'DECISION_SAFETY_EVIDENCE_INVALIDATED';
        END IF;
        previous_close := window_bar.close_time;

        IF EXISTS (
            SELECT 1
            FROM astra_operational_market_bar_conflicts x
            WHERE x.bar_id=window_bar.bar_id
        ) THEN
            RAISE EXCEPTION 'DECISION_SAFETY_EVIDENCE_INVALIDATED';
        END IF;
    END LOOP;

    IF observed_count <> expected_count
       OR first_seen_id <> evidence_row.first_bar_id
       OR last_seen_id <> evidence_row.last_bar_id THEN
        RAISE EXCEPTION 'DECISION_SAFETY_EVIDENCE_INVALIDATED';
    END IF;

    SELECT
        c.provider,
        c.venue,
        c.symbol,
        c.interval_seconds,
        c.through_bar_id,
        c.through_close_time,
        b.close_time AS durable_close_time
    INTO checkpoint_row
    FROM astra_operational_market_continuity c
    JOIN astra_operational_market_bars b ON b.bar_id=c.through_bar_id
    WHERE c.checkpoint_id=evidence_row.checkpoint_id
    FOR UPDATE OF b;

    IF NOT FOUND
       OR checkpoint_row.provider <> evidence_row.provider
       OR checkpoint_row.venue <> evidence_row.venue
       OR checkpoint_row.symbol <> evidence_row.symbol
       OR checkpoint_row.interval_seconds <> evidence_row.interval_seconds
       OR checkpoint_row.through_close_time <> checkpoint_row.durable_close_time
       OR checkpoint_row.through_close_time < evidence_row.ticket_close_time
       OR EXISTS (
            SELECT 1
            FROM astra_operational_market_bar_conflicts x
            WHERE x.bar_id=checkpoint_row.through_bar_id
       ) THEN
        RAISE EXCEPTION 'DECISION_SAFETY_EVIDENCE_INVALIDATED';
    END IF;
END;
$$;

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
