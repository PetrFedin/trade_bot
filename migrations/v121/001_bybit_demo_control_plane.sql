BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_demo_control_event_v121 (
    event_seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id text NOT NULL UNIQUE CHECK (event_id ~ '^[0-9a-f]{64}$'),
    event_kind text NOT NULL CHECK (event_kind IN ('ARM_NEW_ENTRIES', 'HALT_NEW_ENTRIES')),
    operator_id text NOT NULL CHECK (btrim(operator_id) <> '' AND length(operator_id) <= 128),
    reason text NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 1000),
    preflight_status text NULL CHECK (
        preflight_status IS NULL
        OR preflight_status = 'READY_FOR_MANUAL_OPERATOR_APPROVAL'
    ),
    preflight_record_sha256 text NULL CHECK (
        preflight_record_sha256 IS NULL OR preflight_record_sha256 ~ '^[0-9a-f]{64}$'
    ),
    preflight_canonical_record text NULL,
    preflight_observed_at timestamptz NULL,
    armed_until timestamptz NULL,
    created_at timestamptz NOT NULL,
    immutable_record boolean NOT NULL DEFAULT true CHECK (immutable_record = true),
    order_submission_supported boolean NOT NULL DEFAULT false
        CHECK (order_submission_supported = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    CHECK (
        (
            event_kind = 'ARM_NEW_ENTRIES'
            AND preflight_status = 'READY_FOR_MANUAL_OPERATOR_APPROVAL'
            AND preflight_record_sha256 IS NOT NULL
            AND preflight_canonical_record IS NOT NULL
            AND btrim(preflight_canonical_record) <> ''
            AND preflight_observed_at IS NOT NULL
            AND armed_until IS NOT NULL
            AND preflight_observed_at <= created_at
            AND preflight_observed_at >= created_at - interval '30 seconds'
            AND armed_until > created_at
            AND armed_until <= created_at + interval '5 minutes'
        )
        OR
        (
            event_kind = 'HALT_NEW_ENTRIES'
            AND preflight_status IS NULL
            AND preflight_record_sha256 IS NULL
            AND preflight_canonical_record IS NULL
            AND preflight_observed_at IS NULL
            AND armed_until IS NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS astra_bybit_demo_control_latest_idx_v121
    ON astra_bybit_demo_control_event_v121(event_seq DESC);

CREATE OR REPLACE FUNCTION astra_reject_bybit_demo_control_mutation_v121()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit Demo control plane v121 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_demo_control_append_only_v121
    ON astra_bybit_demo_control_event_v121;
CREATE TRIGGER astra_bybit_demo_control_append_only_v121
BEFORE UPDATE OR DELETE ON astra_bybit_demo_control_event_v121
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_demo_control_mutation_v121();

REVOKE ALL ON astra_bybit_demo_control_event_v121 FROM PUBLIC;
REVOKE ALL ON SEQUENCE astra_bybit_demo_control_event_v121_event_seq_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_demo_control_mutation_v121() FROM PUBLIC;

COMMIT;
