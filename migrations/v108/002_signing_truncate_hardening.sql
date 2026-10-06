BEGIN;

-- The frozen v108 migration protects astra_signing_event_v108 against UPDATE and DELETE
-- but not against TRUNCATE, so a principal that owns the table can still erase an
-- append-only audit log through a table-level operation that row triggers never see.
-- That migration and its release hash stay untouched; this is a forward layer, added the
-- way v120 and v121 already add theirs.
DO $$
BEGIN
    IF to_regclass('public.astra_signing_event_v108') IS NULL THEN
        RAISE EXCEPTION 'required v108 append-only table is missing: astra_signing_event_v108';
    END IF;
    IF to_regprocedure('public.astra_signing_event_append_only_v108()') IS NULL THEN
        RAISE EXCEPTION 'required v108 append-only trigger function is missing';
    END IF;
END
$$;

DROP TRIGGER IF EXISTS astra_signing_event_no_truncate_v108 ON astra_signing_event_v108;
CREATE TRIGGER astra_signing_event_no_truncate_v108
BEFORE TRUNCATE ON astra_signing_event_v108
FOR EACH STATEMENT EXECUTE FUNCTION astra_signing_event_append_only_v108();

REVOKE ALL ON astra_signing_event_v108 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_signing_event_append_only_v108() FROM PUBLIC;

COMMIT;
