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

-- Existing installations may already have accepted broker identities. Backfill only
-- evidence that is unambiguous. Never silently choose one order when the same broker
-- identity is present on multiple intents, and never reinterpret a current broker id
-- as a new root when an existing lineage for that intent does not contain it.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM astra_oms_orders
        WHERE broker_order_id <> ''
        GROUP BY broker_order_id
        HAVING count(DISTINCT intent_id) > 1
    ) THEN
        RAISE EXCEPTION 'ambiguous legacy broker_order_id across OMS intents';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM astra_oms_orders AS orders
        JOIN astra_broker_order_identities AS identity
          ON identity.broker_order_id = orders.broker_order_id
        WHERE orders.broker_order_id <> ''
          AND identity.intent_id <> orders.intent_id
    ) THEN
        RAISE EXCEPTION 'legacy broker_order_id conflicts with existing lineage owner';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM astra_oms_orders AS orders
        JOIN astra_broker_order_identities AS root
          ON root.intent_id = orders.intent_id
         AND root.generation = 0
        LEFT JOIN astra_broker_order_identities AS exact_identity
          ON exact_identity.broker_order_id = orders.broker_order_id
         AND exact_identity.intent_id = orders.intent_id
        WHERE orders.broker_order_id <> ''
          AND exact_identity.broker_order_id IS NULL
          AND root.broker_order_id <> orders.broker_order_id
    ) THEN
        RAISE EXCEPTION 'current OMS broker_order_id is not proven by existing lineage';
    END IF;
END;
$$;

INSERT INTO astra_broker_order_identities (
    broker_order_id,
    intent_id,
    predecessor_broker_order_id,
    replace_mutation_id,
    generation,
    created_at
)
SELECT
    orders.broker_order_id,
    orders.intent_id,
    NULL,
    NULL,
    0,
    orders.updated_at
FROM astra_oms_orders AS orders
WHERE orders.broker_order_id <> ''
  AND NOT EXISTS (
      SELECT 1
      FROM astra_broker_order_identities AS identity
      WHERE identity.broker_order_id = orders.broker_order_id
  )
ON CONFLICT (intent_id) WHERE generation = 0 DO NOTHING;

COMMIT;
