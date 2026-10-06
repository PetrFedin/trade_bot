BEGIN;

-- The frozen v107 migration protects astra_rollout_event_v107 against UPDATE and DELETE
-- but not against TRUNCATE, so a principal that owns the table can still erase an
-- append-only audit log through a table-level operation that row triggers never see.
-- That migration and its release hash stay untouched; this is a forward layer, added the
-- way v120 and v121 already add theirs.
DO $$
BEGIN
    IF to_regclass('public.astra_rollout_event_v107') IS NULL THEN
        RAISE EXCEPTION 'required v107 append-only table is missing: astra_rollout_event_v107';
    END IF;
    IF to_regprocedure('public.astra_rollout_event_append_only_v107()') IS NULL THEN
        RAISE EXCEPTION 'required v107 append-only trigger function is missing';
    END IF;
END
$$;

DROP TRIGGER IF EXISTS astra_rollout_event_no_truncate_v107 ON astra_rollout_event_v107;
CREATE TRIGGER astra_rollout_event_no_truncate_v107
BEFORE TRUNCATE ON astra_rollout_event_v107
FOR EACH STATEMENT EXECUTE FUNCTION astra_rollout_event_append_only_v107();

REVOKE ALL ON astra_rollout_event_v107 FROM PUBLIC;
REVOKE ALL ON FUNCTION astra_rollout_event_append_only_v107() FROM PUBLIC;

COMMIT;
