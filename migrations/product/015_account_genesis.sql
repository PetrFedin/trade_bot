BEGIN;

-- The opening cash of an account is its anchor: every P&L figure, reconciliation and
-- equity-relative risk limit is measured from it. Until now it was supplied by the
-- caller on every replay, so reopening durable state with a different configured value
-- silently rewrote the account's starting capital with no cash-flow event and no
-- rejection. Legitimate capital changes already have a path - append_cash_adjustment -
-- and this table is what forces them to use it.
CREATE TABLE IF NOT EXISTS astra_account_genesis (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    opening_cash numeric NOT NULL CHECK (opening_cash > 0),
    bound_at timestamptz NOT NULL
);

-- The genesis becomes immutable when something has been measured from it. An account
-- whose journal is still empty has restated nothing, so it may be reopened at a
-- different figure; once a single portfolio event exists the anchor is fixed and only
-- a cash adjustment may move capital.
CREATE OR REPLACE FUNCTION astra_account_genesis_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM astra_portfolio_events LIMIT 1) THEN
        RAISE EXCEPTION 'astra_account_genesis is immutable once the journal has history';
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS astra_account_genesis_no_update_delete ON astra_account_genesis;
CREATE TRIGGER astra_account_genesis_no_update_delete
BEFORE UPDATE OR DELETE ON astra_account_genesis
FOR EACH ROW EXECUTE FUNCTION astra_account_genesis_immutable();

CREATE OR REPLACE FUNCTION astra_reject_account_genesis_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ASTRA_ACCOUNT_GENESIS_TRUNCATE_FORBIDDEN';
END;
$$;

DROP TRIGGER IF EXISTS astra_account_genesis_no_truncate ON astra_account_genesis;
CREATE TRIGGER astra_account_genesis_no_truncate
BEFORE TRUNCATE ON astra_account_genesis
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_account_genesis_truncate();

COMMIT;
