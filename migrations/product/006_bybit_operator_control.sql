BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_operator_state (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    mode text NOT NULL CHECK (mode IN ('RUNNING', 'PAUSED', 'READ_ONLY', 'KILLED')),
    generation bigint NOT NULL CHECK (generation > 0),
    updated_at timestamptz NOT NULL,
    updated_by text NOT NULL CHECK (length(btrim(updated_by)) BETWEEN 1 AND 128),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 512)
);

INSERT INTO astra_bybit_operator_state(
    singleton,
    mode,
    generation,
    updated_at,
    updated_by,
    reason
)
VALUES (
    TRUE,
    'PAUSED',
    1,
    now(),
    'SYSTEM',
    'INITIAL_FAIL_CLOSED_OPERATOR_STATE'
)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS astra_bybit_operator_actions (
    action_id text PRIMARY KEY CHECK (length(btrim(action_id)) BETWEEN 1 AND 128),
    generation bigint NOT NULL UNIQUE CHECK (generation > 1),
    from_mode text NOT NULL CHECK (from_mode IN ('RUNNING', 'PAUSED', 'READ_ONLY', 'KILLED')),
    to_mode text NOT NULL CHECK (to_mode IN ('RUNNING', 'PAUSED', 'READ_ONLY', 'KILLED')),
    actor text NOT NULL CHECK (length(btrim(actor)) BETWEEN 1 AND 128),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 512),
    occurred_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_astra_bybit_operator_actions_generation
    ON astra_bybit_operator_actions (generation DESC);

CREATE OR REPLACE FUNCTION astra_bybit_operator_actions_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'astra_bybit_operator_actions is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_operator_actions_no_update_delete
    ON astra_bybit_operator_actions;
CREATE TRIGGER astra_bybit_operator_actions_no_update_delete
BEFORE UPDATE OR DELETE ON astra_bybit_operator_actions
FOR EACH ROW EXECUTE FUNCTION astra_bybit_operator_actions_append_only();

COMMIT;
