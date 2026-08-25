BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_demo_session_risk_v122 (
    session_name text PRIMARY KEY CHECK (session_name = 'ACTIVE'),
    opening_equity_usdt numeric NOT NULL CHECK (
        opening_equity_usdt > 0
        AND opening_equity_usdt NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
    ),
    peak_equity_usdt numeric NOT NULL CHECK (
        peak_equity_usdt >= opening_equity_usdt
        AND peak_equity_usdt NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
    ),
    ledger_revision text NOT NULL CHECK (ledger_revision ~ '^[0-9a-f]{64}$'),
    canonical_checkpoint text NOT NULL CHECK (canonical_checkpoint <> ''),
    outcome_count integer NOT NULL CHECK (outcome_count >= 0),
    diagnostics_only boolean NOT NULL DEFAULT true CHECK (diagnostics_only = true),
    order_writes_supported boolean NOT NULL DEFAULT false
        CHECK (order_writes_supported = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
    CHECK (
        (canonical_checkpoint::jsonb ->> 'schema_version')::integer = 1
        AND canonical_checkpoint::jsonb ->> 'kind' = 'BYBIT_DEMO_SESSION_RISK_LEDGER'
        AND (canonical_checkpoint::jsonb ->> 'demo_only')::boolean = true
        AND (
            canonical_checkpoint::jsonb ->> 'live_mainnet_order_routing_allowed'
        )::boolean = false
        AND (
            canonical_checkpoint::jsonb -> 'ledger' ->> 'opening_equity_usdt'
        )::numeric = opening_equity_usdt
        AND (
            canonical_checkpoint::jsonb -> 'ledger' ->> 'peak_equity_usdt'
        )::numeric = peak_equity_usdt
        AND jsonb_array_length(
            canonical_checkpoint::jsonb -> 'ledger' -> 'outcomes'
        ) = outcome_count
        AND canonical_checkpoint::jsonb ->> 'ledger_revision_sha256' = ledger_revision
    )
);

CREATE TABLE IF NOT EXISTS astra_bybit_demo_session_trade_outcome_v122 (
    entry_order_link_id text PRIMARY KEY
        CHECK (entry_order_link_id LIKE 'ASTRA-DEMO-%'),
    symbol text NOT NULL CHECK (symbol ~ '^[A-Z0-9]+USDT$'),
    created_time_ms bigint NOT NULL CHECK (created_time_ms >= 0),
    updated_time_ms bigint NOT NULL CHECK (updated_time_ms >= created_time_ms),
    all_in_net_pnl_usdt numeric NOT NULL CHECK (
        all_in_net_pnl_usdt NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
    ),
    execution_fees_usdt numeric NOT NULL CHECK (
        execution_fees_usdt NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)
    ),
    record_sha256 text NOT NULL CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),
    canonical_record text NOT NULL CHECK (canonical_record <> ''),
    immutable_record boolean NOT NULL DEFAULT true CHECK (immutable_record = true),
    diagnostics_only boolean NOT NULL DEFAULT true CHECK (diagnostics_only = true),
    order_writes_supported boolean NOT NULL DEFAULT false
        CHECK (order_writes_supported = false),
    live_mainnet_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (live_mainnet_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    CHECK (
        canonical_record::jsonb ->> 'entry_order_link_id' = entry_order_link_id
        AND canonical_record::jsonb ->> 'symbol' = symbol
        AND (canonical_record::jsonb ->> 'created_time_ms')::bigint = created_time_ms
        AND (canonical_record::jsonb ->> 'updated_time_ms')::bigint = updated_time_ms
        AND (
            canonical_record::jsonb ->> 'all_in_net_pnl_usdt'
        )::numeric = all_in_net_pnl_usdt
        AND (
            canonical_record::jsonb ->> 'execution_fees_usdt'
        )::numeric = execution_fees_usdt
    )
);

CREATE INDEX IF NOT EXISTS astra_bybit_demo_session_outcome_time_idx_v122
    ON astra_bybit_demo_session_trade_outcome_v122(
        updated_time_ms,
        created_time_ms,
        entry_order_link_id
    );

CREATE OR REPLACE FUNCTION astra_guard_bybit_demo_session_risk_v122()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
        RAISE EXCEPTION 'Bybit Demo session-risk ledger v122 cannot be deleted or truncated';
    END IF;
    IF NEW.session_name <> OLD.session_name
       OR NEW.opening_equity_usdt <> OLD.opening_equity_usdt
       OR NEW.created_at <> OLD.created_at
       OR NEW.diagnostics_only <> OLD.diagnostics_only
       OR NEW.order_writes_supported <> OLD.order_writes_supported
       OR NEW.live_mainnet_order_routing_allowed
          <> OLD.live_mainnet_order_routing_allowed THEN
        RAISE EXCEPTION 'Bybit Demo session-risk identity v122 is immutable';
    END IF;
    IF NEW.peak_equity_usdt < OLD.peak_equity_usdt THEN
        RAISE EXCEPTION 'Bybit Demo session-risk peak equity cannot decrease';
    END IF;
    IF NEW.outcome_count < OLD.outcome_count THEN
        RAISE EXCEPTION 'Bybit Demo session-risk outcomes cannot be removed';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements(
            OLD.canonical_checkpoint::jsonb -> 'ledger' -> 'outcomes'
        ) AS old_outcome
        WHERE NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                NEW.canonical_checkpoint::jsonb -> 'ledger' -> 'outcomes'
            ) AS new_outcome
            WHERE new_outcome ->> 'entry_order_link_id'
                    = old_outcome ->> 'entry_order_link_id'
              AND new_outcome = old_outcome
        )
    ) THEN
        RAISE EXCEPTION 'Bybit Demo session-risk historical outcomes are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION astra_reject_bybit_demo_session_outcome_mutation_v122()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit Demo session-risk outcomes v122 are append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_demo_session_risk_guard_v122
    ON astra_bybit_demo_session_risk_v122;
CREATE TRIGGER astra_bybit_demo_session_risk_guard_v122
BEFORE UPDATE OR DELETE ON astra_bybit_demo_session_risk_v122
FOR EACH ROW EXECUTE FUNCTION astra_guard_bybit_demo_session_risk_v122();

DROP TRIGGER IF EXISTS astra_bybit_demo_session_risk_no_truncate_v122
    ON astra_bybit_demo_session_risk_v122;
CREATE TRIGGER astra_bybit_demo_session_risk_no_truncate_v122
BEFORE TRUNCATE ON astra_bybit_demo_session_risk_v122
FOR EACH STATEMENT EXECUTE FUNCTION astra_guard_bybit_demo_session_risk_v122();

DROP TRIGGER IF EXISTS astra_bybit_demo_session_outcome_append_only_v122
    ON astra_bybit_demo_session_trade_outcome_v122;
CREATE TRIGGER astra_bybit_demo_session_outcome_append_only_v122
BEFORE UPDATE OR DELETE ON astra_bybit_demo_session_trade_outcome_v122
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_demo_session_outcome_mutation_v122();

DROP TRIGGER IF EXISTS astra_bybit_demo_session_outcome_no_truncate_v122
    ON astra_bybit_demo_session_trade_outcome_v122;
CREATE TRIGGER astra_bybit_demo_session_outcome_no_truncate_v122
BEFORE TRUNCATE ON astra_bybit_demo_session_trade_outcome_v122
FOR EACH STATEMENT
EXECUTE FUNCTION astra_reject_bybit_demo_session_outcome_mutation_v122();

REVOKE ALL ON astra_bybit_demo_session_risk_v122 FROM PUBLIC;
REVOKE ALL ON astra_bybit_demo_session_trade_outcome_v122 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_guard_bybit_demo_session_risk_v122() FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_demo_session_outcome_mutation_v122()
    FROM PUBLIC;

COMMIT;
