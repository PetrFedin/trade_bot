BEGIN;

DO $$
BEGIN
    IF to_regclass('public.astra_bybit_demo_control_event_v121') IS NULL THEN
        RAISE EXCEPTION 'required Bybit Demo v121 control journal table is missing';
    END IF;
    IF to_regprocedure('public.astra_reject_bybit_demo_control_mutation_v121()') IS NULL THEN
        RAISE EXCEPTION 'required Bybit Demo v121 append-only trigger function is missing';
    END IF;
END
$$;

DROP TRIGGER IF EXISTS astra_bybit_demo_control_no_truncate_v121
    ON astra_bybit_demo_control_event_v121;
CREATE TRIGGER astra_bybit_demo_control_no_truncate_v121
BEFORE TRUNCATE ON astra_bybit_demo_control_event_v121
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_bybit_demo_control_mutation_v121();

REVOKE ALL ON astra_bybit_demo_control_event_v121 FROM PUBLIC;
REVOKE ALL ON SEQUENCE astra_bybit_demo_control_event_v121_event_seq_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_demo_control_mutation_v121() FROM PUBLIC;

COMMIT;
