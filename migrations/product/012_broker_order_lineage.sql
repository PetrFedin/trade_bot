BEGIN;

CREATE TABLE IF NOT EXISTS astra_broker_order_identities (
    broker_order_id text PRIMARY KEY,
    intent_id text NOT NULL REFERENCES astra_oms_orders(intent_id) ON DELETE RESTRICT,
    predecessor_broker_order_id text
        REFERENCES astra_broker_order_identities(broker_order_id) ON DELETE RESTRICT,
    replace_mutation_id text UNIQUE,
    generation bigint NOT NULL CHECK (generation >= 0),
    created_at timestamptz NOT NULL,
    CHECK (
        (generation = 0 AND predecessor_broker_order_id IS NULL AND replace_mutation_id IS NULL)
        OR
        (generation > 0 AND predecessor_broker_order_id IS NOT NULL AND replace_mutation_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_astra_broker_order_identity_primary
    ON astra_broker_order_identities (intent_id)
    WHERE generation = 0;

CREATE UNIQUE INDEX IF NOT EXISTS idx_astra_broker_order_identity_successor
    ON astra_broker_order_identities (predecessor_broker_order_id)
    WHERE predecessor_broker_order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_astra_broker_order_identity_intent_generation
    ON astra_broker_order_identities (intent_id, generation);

CREATE OR REPLACE FUNCTION astra_broker_order_identities_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_broker_order_identities is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_broker_order_identities_no_update_delete
    ON astra_broker_order_identities;
CREATE TRIGGER astra_broker_order_identities_no_update_delete
BEFORE UPDATE OR DELETE ON astra_broker_order_identities
FOR EACH ROW EXECUTE FUNCTION astra_broker_order_identities_append_only();

-- Existing installations may already have accepted broker identities. Treat the
-- currently stored id as the legacy root. Historical predecessors cannot be
-- reconstructed safely and are therefore not invented by this migration.
INSERT INTO astra_broker_order_identities (
    broker_order_id,
    intent_id,
    predecessor_broker_order_id,
    replace_mutation_id,
    generation,
    created_at
)
SELECT
    broker_order_id,
    intent_id,
    NULL,
    NULL,
    0,
    updated_at
FROM astra_oms_orders
WHERE broker_order_id <> ''
ON CONFLICT (broker_order_id) DO NOTHING;

COMMIT;
