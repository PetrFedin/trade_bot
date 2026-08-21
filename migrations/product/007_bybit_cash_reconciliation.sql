BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_cash_baseline (
    baseline_key text PRIMARY KEY,
    currency text NOT NULL CHECK (currency = 'USDT'),
    wallet_balance_usdt numeric NOT NULL,
    cumulative_all_in_pnl_usdt numeric NOT NULL,
    session_revision char(64) NOT NULL,
    created_time_ms bigint NOT NULL CHECK (created_time_ms >= 0),
    created_at timestamptz NOT NULL,
    CHECK (baseline_key = 'USDT')
);

CREATE OR REPLACE FUNCTION astra_bybit_cash_baseline_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_bybit cash baseline is immutable';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_cash_baseline_no_update_delete
    ON astra_bybit_cash_baseline;
CREATE TRIGGER astra_bybit_cash_baseline_no_update_delete
BEFORE UPDATE OR DELETE ON astra_bybit_cash_baseline
FOR EACH ROW EXECUTE FUNCTION astra_bybit_cash_baseline_immutable();

COMMIT;
