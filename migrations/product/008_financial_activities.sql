CREATE TABLE IF NOT EXISTS astra_financial_activity_facts (
    account_identity TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    activity_type TEXT NOT NULL,
    net_amount NUMERIC NOT NULL,
    currency TEXT NOT NULL,
    symbol TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    first_seen_release_identity TEXT NOT NULL,
    source_cursor TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    canonical_payload JSONB NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_identity, activity_id)
);

CREATE TABLE IF NOT EXISTS astra_financial_activity_projection (
    account_identity TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING', 'PROJECTED', 'QUARANTINED')),
    reason TEXT,
    portfolio_event_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_identity, activity_id),
    FOREIGN KEY (account_identity, activity_id)
        REFERENCES astra_financial_activity_facts(account_identity, activity_id)
);

CREATE TABLE IF NOT EXISTS astra_financial_activity_conflicts (
    sequence BIGSERIAL PRIMARY KEY,
    account_identity TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    existing_payload_hash TEXT NOT NULL,
    observed_payload_hash TEXT NOT NULL,
    observed_payload JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_financial_activity_recovery (
    account_identity TEXT NOT NULL,
    release_identity TEXT NOT NULL,
    recovered_through TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_identity, release_identity)
);

CREATE INDEX IF NOT EXISTS idx_financial_projection_state
ON astra_financial_activity_projection(account_identity, state, updated_at, activity_id);

CREATE INDEX IF NOT EXISTS idx_financial_fact_time
ON astra_financial_activity_facts(account_identity, occurred_at, activity_id);

CREATE OR REPLACE FUNCTION reject_astra_financial_fact_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'astra_financial_activity_facts is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_financial_activity_facts_no_update ON astra_financial_activity_facts;
CREATE TRIGGER astra_financial_activity_facts_no_update
BEFORE UPDATE ON astra_financial_activity_facts
FOR EACH ROW EXECUTE FUNCTION reject_astra_financial_fact_mutation();

DROP TRIGGER IF EXISTS astra_financial_activity_facts_no_delete ON astra_financial_activity_facts;
CREATE TRIGGER astra_financial_activity_facts_no_delete
BEFORE DELETE ON astra_financial_activity_facts
FOR EACH ROW EXECUTE FUNCTION reject_astra_financial_fact_mutation();

CREATE OR REPLACE FUNCTION reject_astra_financial_conflict_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'astra_financial_activity_conflicts is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_financial_activity_conflicts_no_update ON astra_financial_activity_conflicts;
CREATE TRIGGER astra_financial_activity_conflicts_no_update
BEFORE UPDATE ON astra_financial_activity_conflicts
FOR EACH ROW EXECUTE FUNCTION reject_astra_financial_conflict_mutation();

DROP TRIGGER IF EXISTS astra_financial_activity_conflicts_no_delete ON astra_financial_activity_conflicts;
CREATE TRIGGER astra_financial_activity_conflicts_no_delete
BEFORE DELETE ON astra_financial_activity_conflicts
FOR EACH ROW EXECUTE FUNCTION reject_astra_financial_conflict_mutation();
