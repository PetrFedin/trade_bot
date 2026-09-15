BEGIN;

CREATE TABLE IF NOT EXISTS astra_operational_market_bars (
    bar_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    open_time TIMESTAMPTZ NOT NULL,
    close_time TIMESTAMPTZ NOT NULL,
    source_timestamp TIMESTAMPTZ NOT NULL,
    first_received_at TIMESTAMPTZ NOT NULL,
    source_event_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    open_price NUMERIC NOT NULL,
    high_price NUMERIC NOT NULL,
    low_price NUMERIC NOT NULL,
    close_price NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    content_hash TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operational_market_bars_window
ON astra_operational_market_bars(
    provider, venue, symbol, interval_seconds, close_time, bar_id
);

CREATE TABLE IF NOT EXISTS astra_operational_market_bar_conflicts (
    sequence BIGSERIAL PRIMARY KEY,
    bar_id TEXT NOT NULL,
    existing_content_hash TEXT NOT NULL,
    observed_content_hash TEXT NOT NULL,
    observed_payload JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_operational_decision_tickets (
    ticket_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    bar_id TEXT NOT NULL REFERENCES astra_operational_market_bars(bar_id),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(strategy_id, bar_id)
);

CREATE INDEX IF NOT EXISTS idx_operational_decision_tickets_strategy
ON astra_operational_decision_tickets(strategy_id, created_at, ticket_id);

CREATE TABLE IF NOT EXISTS astra_operational_decision_completions (
    ticket_id TEXT PRIMARY KEY REFERENCES astra_operational_decision_tickets(ticket_id),
    outcome_id TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION reject_astra_operational_market_bar_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'astra_operational_market_bars is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_operational_market_bars_no_update
ON astra_operational_market_bars;
CREATE TRIGGER astra_operational_market_bars_no_update
BEFORE UPDATE ON astra_operational_market_bars
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_market_bar_mutation();

DROP TRIGGER IF EXISTS astra_operational_market_bars_no_delete
ON astra_operational_market_bars;
CREATE TRIGGER astra_operational_market_bars_no_delete
BEFORE DELETE ON astra_operational_market_bars
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_market_bar_mutation();

CREATE OR REPLACE FUNCTION reject_astra_operational_market_conflict_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'astra_operational_market_bar_conflicts is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_operational_market_conflicts_no_update
ON astra_operational_market_bar_conflicts;
CREATE TRIGGER astra_operational_market_conflicts_no_update
BEFORE UPDATE ON astra_operational_market_bar_conflicts
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_market_conflict_mutation();

DROP TRIGGER IF EXISTS astra_operational_market_conflicts_no_delete
ON astra_operational_market_bar_conflicts;
CREATE TRIGGER astra_operational_market_conflicts_no_delete
BEFORE DELETE ON astra_operational_market_bar_conflicts
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_market_conflict_mutation();

CREATE OR REPLACE FUNCTION reject_astra_operational_decision_ticket_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'astra_operational_decision_tickets is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_operational_decision_tickets_no_update
ON astra_operational_decision_tickets;
CREATE TRIGGER astra_operational_decision_tickets_no_update
BEFORE UPDATE ON astra_operational_decision_tickets
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_decision_ticket_mutation();

DROP TRIGGER IF EXISTS astra_operational_decision_tickets_no_delete
ON astra_operational_decision_tickets;
CREATE TRIGGER astra_operational_decision_tickets_no_delete
BEFORE DELETE ON astra_operational_decision_tickets
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_decision_ticket_mutation();

CREATE OR REPLACE FUNCTION reject_astra_operational_decision_completion_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'astra_operational_decision_completions is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_operational_decision_completions_no_update
ON astra_operational_decision_completions;
CREATE TRIGGER astra_operational_decision_completions_no_update
BEFORE UPDATE ON astra_operational_decision_completions
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_decision_completion_mutation();

DROP TRIGGER IF EXISTS astra_operational_decision_completions_no_delete
ON astra_operational_decision_completions;
CREATE TRIGGER astra_operational_decision_completions_no_delete
BEFORE DELETE ON astra_operational_decision_completions
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_decision_completion_mutation();

COMMIT;
