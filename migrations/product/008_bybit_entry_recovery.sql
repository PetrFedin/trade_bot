BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_entry_recovery (
    entry_order_link_id text PRIMARY KEY,
    record_sha256 char(64) NOT NULL,
    envelope_text text NOT NULL,
    created_at timestamptz NOT NULL,
    CHECK (entry_order_link_id LIKE 'ASTRA-DEMO-E-%')
);

CREATE OR REPLACE FUNCTION astra_bybit_entry_recovery_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_bybit entry recovery envelope is immutable';
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'astra_bybit_entry_recovery_no_update_delete'
          AND tgrelid = 'astra_bybit_entry_recovery'::regclass
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER astra_bybit_entry_recovery_no_update_delete
        BEFORE UPDATE OR DELETE ON astra_bybit_entry_recovery
        FOR EACH ROW EXECUTE FUNCTION astra_bybit_entry_recovery_immutable();
    END IF;
END;
$$;

COMMIT;
