CREATE TABLE IF NOT EXISTS paper_account_reconciliation_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    matched BOOLEAN NOT NULL,
    cash_delta NUMERIC NOT NULL,
    position_mismatches INTEGER NOT NULL CHECK (position_mismatches >= 0),
    reasons JSONB NOT NULL,
    broker_cash NUMERIC NOT NULL CHECK (broker_cash >= 0),
    portfolio_fingerprint TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION reject_paper_account_reconciliation_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'paper_account_reconciliation_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS paper_account_reconciliation_events_no_update
ON paper_account_reconciliation_events;
CREATE TRIGGER paper_account_reconciliation_events_no_update
BEFORE UPDATE ON paper_account_reconciliation_events
FOR EACH ROW EXECUTE FUNCTION reject_paper_account_reconciliation_event_mutation();

DROP TRIGGER IF EXISTS paper_account_reconciliation_events_no_delete
ON paper_account_reconciliation_events;
CREATE TRIGGER paper_account_reconciliation_events_no_delete
BEFORE DELETE ON paper_account_reconciliation_events
FOR EACH ROW EXECUTE FUNCTION reject_paper_account_reconciliation_event_mutation();
