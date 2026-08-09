BEGIN;

CREATE TABLE IF NOT EXISTS astra_portfolio_events (
    sequence bigserial PRIMARY KEY,
    event_id text NOT NULL UNIQUE,
    event_type text NOT NULL CHECK (event_type IN ('FILL', 'SPLIT', 'CASH_DIVIDEND')),
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_portfolio_snapshots (
    snapshot_id bigserial PRIMARY KEY,
    occurred_at timestamptz NOT NULL,
    payload jsonb NOT NULL
);

CREATE OR REPLACE FUNCTION astra_portfolio_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

DROP TRIGGER IF EXISTS astra_portfolio_events_no_update_delete ON astra_portfolio_events;
CREATE TRIGGER astra_portfolio_events_no_update_delete
BEFORE UPDATE OR DELETE ON astra_portfolio_events
FOR EACH ROW EXECUTE FUNCTION astra_portfolio_append_only();

DROP TRIGGER IF EXISTS astra_portfolio_snapshots_no_update_delete ON astra_portfolio_snapshots;
CREATE TRIGGER astra_portfolio_snapshots_no_update_delete
BEFORE UPDATE OR DELETE ON astra_portfolio_snapshots
FOR EACH ROW EXECUTE FUNCTION astra_portfolio_append_only();

COMMIT;
