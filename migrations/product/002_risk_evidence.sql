BEGIN;

CREATE TABLE IF NOT EXISTS astra_risk_chain_state (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    last_sequence bigint NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    last_digest text NOT NULL DEFAULT repeat('0', 64) CHECK (length(last_digest) = 64)
);

INSERT INTO astra_risk_chain_state(singleton, last_sequence, last_digest)
VALUES (TRUE, 0, repeat('0', 64))
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS astra_risk_decisions (
    sequence bigint PRIMARY KEY CHECK (sequence > 0),
    decision_id text NOT NULL UNIQUE,
    intent_id text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    previous_digest text NOT NULL CHECK (length(previous_digest) = 64),
    digest text NOT NULL UNIQUE CHECK (length(digest) = 64)
);

CREATE OR REPLACE FUNCTION astra_risk_decisions_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_risk_decisions is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_risk_decisions_no_update_delete ON astra_risk_decisions;
CREATE TRIGGER astra_risk_decisions_no_update_delete
BEFORE UPDATE OR DELETE ON astra_risk_decisions
FOR EACH ROW EXECUTE FUNCTION astra_risk_decisions_append_only();

COMMIT;
