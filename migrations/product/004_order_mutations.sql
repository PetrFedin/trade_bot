BEGIN;

CREATE TABLE IF NOT EXISTS astra_order_mutations (
    mutation_id text PRIMARY KEY,
    intent_id text NOT NULL REFERENCES astra_oms_orders(intent_id) ON DELETE RESTRICT,
    kind text NOT NULL CHECK (kind IN ('CANCEL', 'REPLACE')),
    target_limit_price numeric,
    baseline_limit_price numeric NOT NULL CHECK (baseline_limit_price > 0),
    broker_order_id text NOT NULL CHECK (broker_order_id <> ''),
    state text NOT NULL CHECK (
        state IN ('REQUESTED', 'STARTED', 'SUCCEEDED', 'FAILED', 'UNCERTAIN')
    ),
    outcome text NOT NULL DEFAULT '',
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (
        (kind = 'CANCEL' AND target_limit_price IS NULL)
        OR (kind = 'REPLACE' AND target_limit_price IS NOT NULL AND target_limit_price > 0)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_astra_order_mutations_one_active
    ON astra_order_mutations (intent_id)
    WHERE state IN ('REQUESTED', 'STARTED', 'UNCERTAIN');

CREATE TABLE IF NOT EXISTS astra_order_mutation_events (
    event_id text PRIMARY KEY,
    mutation_id text NOT NULL
        REFERENCES astra_order_mutations(mutation_id) ON DELETE RESTRICT,
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_order_mutation_outbox (
    message_id bigserial PRIMARY KEY,
    mutation_id text NOT NULL UNIQUE
        REFERENCES astra_order_mutations(mutation_id) ON DELETE RESTRICT,
    intent_id text NOT NULL REFERENCES astra_oms_orders(intent_id) ON DELETE RESTRICT,
    topic text NOT NULL CHECK (topic IN ('paper_order_cancel', 'paper_order_replace')),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    published_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_astra_order_mutation_outbox_pending
    ON astra_order_mutation_outbox (message_id)
    WHERE published_at IS NULL;

CREATE OR REPLACE FUNCTION astra_order_mutation_events_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_order_mutation_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_order_mutation_events_no_update_delete
    ON astra_order_mutation_events;
CREATE TRIGGER astra_order_mutation_events_no_update_delete
BEFORE UPDATE OR DELETE ON astra_order_mutation_events
FOR EACH ROW EXECUTE FUNCTION astra_order_mutation_events_append_only();

COMMIT;
