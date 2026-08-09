BEGIN;

CREATE TABLE IF NOT EXISTS astra_oms_orders (
    intent_id text PRIMARY KEY,
    client_order_id text NOT NULL UNIQUE,
    broker_order_id text NOT NULL DEFAULT '',
    symbol text NOT NULL CHECK (symbol <> '' AND symbol = upper(symbol)),
    side text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity numeric NOT NULL CHECK (quantity > 0),
    limit_price numeric NOT NULL CHECK (limit_price > 0),
    filled_quantity numeric NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    state text NOT NULL,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL,
    CHECK (filled_quantity <= quantity)
);

CREATE TABLE IF NOT EXISTS astra_oms_events (
    event_id text PRIMARY KEY,
    intent_id text NOT NULL REFERENCES astra_oms_orders(intent_id) ON DELETE RESTRICT,
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS astra_oms_outbox (
    message_id bigserial PRIMARY KEY,
    intent_id text NOT NULL REFERENCES astra_oms_orders(intent_id) ON DELETE RESTRICT,
    topic text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    published_at timestamptz,
    UNIQUE (intent_id, topic)
);

CREATE INDEX IF NOT EXISTS idx_astra_oms_outbox_pending
    ON astra_oms_outbox (message_id)
    WHERE published_at IS NULL;

CREATE OR REPLACE FUNCTION astra_oms_events_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_oms_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_oms_events_no_update_delete ON astra_oms_events;
CREATE TRIGGER astra_oms_events_no_update_delete
BEFORE UPDATE OR DELETE ON astra_oms_events
FOR EACH ROW EXECUTE FUNCTION astra_oms_events_append_only();

COMMIT;
