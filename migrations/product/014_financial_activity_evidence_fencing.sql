BEGIN;

CREATE OR REPLACE FUNCTION astra_reject_financial_activity_evidence_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ASTRA_FINANCIAL_ACTIVITY_EVIDENCE_TRUNCATE_FORBIDDEN';
END;
$$;

DROP TRIGGER IF EXISTS astra_financial_activity_facts_no_truncate
    ON astra_financial_activity_facts;
CREATE TRIGGER astra_financial_activity_facts_no_truncate
BEFORE TRUNCATE ON astra_financial_activity_facts
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_financial_activity_evidence_truncate();

DROP TRIGGER IF EXISTS astra_financial_activity_conflicts_no_truncate
    ON astra_financial_activity_conflicts;
CREATE TRIGGER astra_financial_activity_conflicts_no_truncate
BEFORE TRUNCATE ON astra_financial_activity_conflicts
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_financial_activity_evidence_truncate();

COMMIT;
