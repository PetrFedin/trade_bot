BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_entry_recovery (
    entry_order_link_id text PRIMARY KEY,
    record_sha256 char(64) NOT NULL,
    envelope_text text NOT NULL,
    created_at timestamptz NOT NULL,
    CHECK (entry_order_link_id LIKE 'ASTRA-DEMO-E-%')
);

CREATE FUNCTION astra_bybit_entry_recovery_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_bybit entry recovery envelope is immutable';
END;
$$;

CREATE TRIGGER astra_bybit_entry_recovery_no_update_delete
BEFORE UPDATE OR DELETE ON astra_bybit_entry_recovery
FOR EACH ROW EXECUTE FUNCTION astra_bybit_entry_recovery_immutable();

COMMIT;
