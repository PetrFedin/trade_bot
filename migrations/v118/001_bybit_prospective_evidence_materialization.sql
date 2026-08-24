BEGIN;

CREATE TABLE IF NOT EXISTS astra_bybit_prospective_exact_cell_report_v118 (
    report_id text PRIMARY KEY CHECK (report_id ~ '^[0-9a-f]{64}$'),
    report_generated_at timestamptz NOT NULL,
    report_window text NOT NULL CHECK (report_window <> ''),
    source_observation_count integer NOT NULL CHECK (source_observation_count >= 0),
    source_seed_set_sha256 text NOT NULL CHECK (source_seed_set_sha256 ~ '^[0-9a-f]{64}$'),
    earliest_signal_available_at timestamptz NULL,
    latest_signal_available_at timestamptz NULL,
    source_cell_complete_count integer NOT NULL CHECK (source_cell_complete_count >= 0),
    source_cell_unavailable_count integer NOT NULL CHECK (source_cell_unavailable_count >= 0),
    liquidation_coverage_qualified_count integer NOT NULL
        CHECK (liquidation_coverage_qualified_count >= 0),
    report_json jsonb NOT NULL,
    source_lineage_complete boolean NOT NULL DEFAULT true
        CHECK (source_lineage_complete = true),
    research_only boolean NOT NULL DEFAULT true CHECK (research_only = true),
    trade_actionable boolean NOT NULL DEFAULT false CHECK (trade_actionable = false),
    strategy_promotion_allowed boolean NOT NULL DEFAULT false
        CHECK (strategy_promotion_allowed = false),
    demo_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (demo_activation_allowed = false),
    live_activation_allowed boolean NOT NULL DEFAULT false
        CHECK (live_activation_allowed = false),
    bybit_live_order_routing_allowed boolean NOT NULL DEFAULT false
        CHECK (bybit_live_order_routing_allowed = false),
    created_at timestamptz NOT NULL,
    CHECK (source_cell_complete_count + source_cell_unavailable_count = source_observation_count),
    CHECK (liquidation_coverage_qualified_count <= source_observation_count),
    CHECK (
        (source_observation_count = 0
         AND earliest_signal_available_at IS NULL
         AND latest_signal_available_at IS NULL)
        OR
        (source_observation_count > 0
         AND earliest_signal_available_at IS NOT NULL
         AND latest_signal_available_at IS NOT NULL
         AND earliest_signal_available_at <= latest_signal_available_at)
    )
);

CREATE INDEX IF NOT EXISTS astra_bybit_prospective_exact_cell_report_generated_idx_v118
    ON astra_bybit_prospective_exact_cell_report_v118(
        report_generated_at DESC,
        report_id
    );
CREATE INDEX IF NOT EXISTS astra_bybit_prospective_exact_cell_report_lineage_idx_v118
    ON astra_bybit_prospective_exact_cell_report_v118(
        source_seed_set_sha256,
        report_generated_at DESC
    );

CREATE OR REPLACE FUNCTION astra_reject_bybit_prospective_exact_cell_report_mutation_v118()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Bybit prospective exact-cell report v118 is append-only';
END;
$$;

DROP TRIGGER IF EXISTS astra_bybit_prospective_exact_cell_report_append_only_v118
    ON astra_bybit_prospective_exact_cell_report_v118;
CREATE TRIGGER astra_bybit_prospective_exact_cell_report_append_only_v118
BEFORE UPDATE OR DELETE ON astra_bybit_prospective_exact_cell_report_v118
FOR EACH ROW EXECUTE FUNCTION astra_reject_bybit_prospective_exact_cell_report_mutation_v118();

REVOKE ALL ON astra_bybit_prospective_exact_cell_report_v118 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_prospective_exact_cell_report_mutation_v118()
    FROM PUBLIC;

COMMIT;
