BEGIN;

DO $$
DECLARE
    required_table text;
BEGIN
    FOREACH required_table IN ARRAY ARRAY[
        'astra_bybit_demo_approved_entry_authorization_v120',
        'astra_bybit_demo_entry_provenance_v120',
        'astra_bybit_demo_terminal_evidence_v120'
    ]
    LOOP
        IF to_regclass(format('public.%I', required_table)) IS NULL THEN
            RAISE EXCEPTION 'required Bybit Demo v120 audit table is missing: %', required_table;
        END IF;
    END LOOP;

    IF to_regprocedure('public.astra_reject_bybit_demo_audit_mutation_v120()') IS NULL THEN
        RAISE EXCEPTION 'required Bybit Demo v120 append-only trigger function is missing';
    END IF;
END
$$;

DROP TRIGGER IF EXISTS astra_bybit_demo_approval_no_truncate_v120
    ON astra_bybit_demo_approved_entry_authorization_v120;
CREATE TRIGGER astra_bybit_demo_approval_no_truncate_v120
BEFORE TRUNCATE ON astra_bybit_demo_approved_entry_authorization_v120
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_bybit_demo_audit_mutation_v120();

DROP TRIGGER IF EXISTS astra_bybit_demo_provenance_no_truncate_v120
    ON astra_bybit_demo_entry_provenance_v120;
CREATE TRIGGER astra_bybit_demo_provenance_no_truncate_v120
BEFORE TRUNCATE ON astra_bybit_demo_entry_provenance_v120
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_bybit_demo_audit_mutation_v120();

DROP TRIGGER IF EXISTS astra_bybit_demo_terminal_no_truncate_v120
    ON astra_bybit_demo_terminal_evidence_v120;
CREATE TRIGGER astra_bybit_demo_terminal_no_truncate_v120
BEFORE TRUNCATE ON astra_bybit_demo_terminal_evidence_v120
FOR EACH STATEMENT EXECUTE FUNCTION astra_reject_bybit_demo_audit_mutation_v120();

REVOKE ALL ON astra_bybit_demo_approved_entry_authorization_v120 FROM PUBLIC;
REVOKE ALL ON astra_bybit_demo_entry_provenance_v120 FROM PUBLIC;
REVOKE ALL ON astra_bybit_demo_terminal_evidence_v120 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_reject_bybit_demo_audit_mutation_v120() FROM PUBLIC;

COMMIT;
