BEGIN;

CREATE TABLE IF NOT EXISTS astra_operational_market_continuity (
    checkpoint_id TEXT PRIMARY KEY,
    previous_checkpoint_id TEXT,
    provider TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    through_bar_id TEXT NOT NULL,
    through_close_time TIMESTAMPTZ NOT NULL,
    established_at TIMESTAMPTZ NOT NULL,
    evidence_source TEXT NOT NULL,
    FOREIGN KEY (previous_checkpoint_id)
        REFERENCES astra_operational_market_continuity(checkpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_operational_market_continuity_latest
ON astra_operational_market_continuity(
    provider, venue, symbol, interval_seconds,
    through_close_time DESC, checkpoint_id DESC
);

CREATE OR REPLACE FUNCTION reject_astra_operational_market_continuity_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'astra_operational_market_continuity is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_operational_market_continuity_no_update
ON astra_operational_market_continuity;
CREATE TRIGGER astra_operational_market_continuity_no_update
BEFORE UPDATE ON astra_operational_market_continuity
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_market_continuity_mutation();

DROP TRIGGER IF EXISTS astra_operational_market_continuity_no_delete
ON astra_operational_market_continuity;
CREATE TRIGGER astra_operational_market_continuity_no_delete
BEFORE DELETE ON astra_operational_market_continuity
FOR EACH ROW EXECUTE FUNCTION reject_astra_operational_market_continuity_mutation();

COMMIT;
