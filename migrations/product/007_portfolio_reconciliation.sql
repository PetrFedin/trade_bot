CREATE TABLE IF NOT EXISTS astra_portfolio_reconciliations (
    reconciliation_id TEXT PRIMARY KEY CHECK (length(btrim(reconciliation_id)) > 0),
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_astra_portfolio_reconciliations_time
ON astra_portfolio_reconciliations(occurred_at, reconciliation_id);

CREATE OR REPLACE FUNCTION astra_reject_portfolio_reconciliation_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ASTRA_PORTFOLIO_RECONCILIATION_APPEND_ONLY';
END;
$$;

DROP TRIGGER IF EXISTS astra_portfolio_reconciliations_append_only
ON astra_portfolio_reconciliations;
CREATE TRIGGER astra_portfolio_reconciliations_append_only
BEFORE UPDATE OR DELETE ON astra_portfolio_reconciliations
FOR EACH ROW EXECUTE FUNCTION astra_reject_portfolio_reconciliation_mutation();

CREATE OR REPLACE FUNCTION astra_reject_portfolio_reconciliation_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ASTRA_PORTFOLIO_RECONCILIATION_TRUNCATE_FORBIDDEN';
END;
$$;

DROP TRIGGER IF EXISTS astra_portfolio_reconciliations_no_truncate
ON astra_portfolio_reconciliations;
CREATE TRIGGER astra_portfolio_reconciliations_no_truncate
BEFORE TRUNCATE ON astra_portfolio_reconciliations
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_portfolio_reconciliation_truncate();
