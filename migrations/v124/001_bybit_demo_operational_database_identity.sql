BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_demo_operational_identity_v124 (
    identity_name text PRIMARY KEY
        CHECK (identity_name = 'CANONICAL_DEMO_OPERATIONAL_DATABASE'),
    database_instance_id uuid NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    immutable_record boolean NOT NULL DEFAULT true CHECK (immutable_record = true),
    diagnostics_only boolean NOT NULL DEFAULT true CHECK (diagnostics_only = true),
    order_writes_supported boolean NOT NULL DEFAULT false
        CHECK (order_writes_supported = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false)
);

INSERT INTO astra_bybit_demo_operational_identity_v124(
    identity_name,
    database_instance_id,
    created_at,
    immutable_record,
    diagnostics_only,
    order_writes_supported,
    live_mainnet_order_routing_allowed
)
VALUES (
    'CANONICAL_DEMO_OPERATIONAL_DATABASE',
    gen_random_uuid(),
    now(),
    true,
    true,
    false,
    false
)
ON CONFLICT (identity_name) DO NOTHING;

CREATE OR REPLACE FUNCTION astra_reject_bybit_demo_operational_identity_mutation_v124()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit Demo operational database identity v124 is immutable';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_demo_operational_identity_immutable_v124
    ON astra_bybit_demo_operational_identity_v124;
CREATE TRIGGER astra_bybit_demo_operational_identity_immutable_v124
BEFORE UPDATE OR DELETE ON astra_bybit_demo_operational_identity_v124
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_demo_operational_identity_mutation_v124();

DROP TRIGGER IF EXISTS astra_bybit_demo_operational_identity_no_truncate_v124
    ON astra_bybit_demo_operational_identity_v124;
CREATE TRIGGER astra_bybit_demo_operational_identity_no_truncate_v124
BEFORE TRUNCATE ON astra_bybit_demo_operational_identity_v124
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_bybit_demo_operational_identity_mutation_v124();

REVOKE ALL ON astra_bybit_demo_operational_identity_v124 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_demo_operational_identity_mutation_v124() FROM PUBLIC;

COMMIT;
