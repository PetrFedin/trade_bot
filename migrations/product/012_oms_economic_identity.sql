BEGIN;

ALTER TABLE astra_oms_orders
    ADD COLUMN IF NOT EXISTS intent_fingerprint text;

ALTER TABLE astra_oms_events
    ADD COLUMN IF NOT EXISTS event_fingerprint text;

COMMENT ON COLUMN astra_oms_orders.intent_fingerprint IS
    'SHA-256 binding of immutable OrderIntent identity/economics. NULL means legacy identity is not fully proven.';

COMMENT ON COLUMN astra_oms_events.event_fingerprint IS
    'SHA-256 binding of intent id, event type, payload and declared broker order identity. NULL means legacy event identity is not fully proven.';

COMMIT;
