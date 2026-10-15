"""Coverage for #109: the frozen v107-v109 audit logs reject TRUNCATE.

The frozen migrations guard those tables against UPDATE and DELETE only. A row-level
trigger never sees a table-level TRUNCATE, so a principal that owns the table could
still erase an append-only audit log, and the CI contract could report PASS while that
was true because information_schema.triggers reports only INSERT, UPDATE and DELETE.

The frozen migrations and their release hashes stay untouched; the protection is a
forward 002 layer, the way v120 and v121 already add theirs.

v109 is deliberately absent. Its audit log is truncated by
tests/test_postgres_remote_signer_repository_v109.py, which is one of six test files
covered by the release identity digest, so hardening it means superseding a frozen test.
That is a release-identity decision rather than a code change, and it is recorded in the
pull request instead of being forced here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARDENED = (
    (
        "v107",
        ROOT / "migrations/v107/002_rollout_truncate_hardening.sql",
        "astra_rollout_event_v107",
        "astra_rollout_event_no_truncate_v107",
        "astra_rollout_event_append_only_v107",
    ),
    (
        "v108",
        ROOT / "migrations/v108/002_signing_truncate_hardening.sql",
        "astra_signing_event_v108",
        "astra_signing_event_no_truncate_v108",
        "astra_signing_event_append_only_v108",
    ),
)

FROZEN = (
    ROOT / "migrations/v107/001_production_rollout_actuator.sql",
    ROOT / "migrations/v108/001_asymmetric_signing_authority.sql",
)


@pytest.mark.parametrize(("version", "path", "table", "trigger", "function"), HARDENED)
def test_forward_layer_guards_truncate(
    version: str, path: Path, table: str, trigger: str, function: str
) -> None:
    sql = path.read_text(encoding="utf-8")
    assert f"BEFORE TRUNCATE ON {table}" in sql
    assert f"CREATE TRIGGER {trigger}" in sql
    assert f"EXECUTE FUNCTION {function}()" in sql
    assert "FOR EACH STATEMENT" in sql


@pytest.mark.parametrize(("version", "path", "table", "trigger", "function"), HARDENED)
def test_forward_layer_refuses_to_run_against_a_missing_schema(
    version: str, path: Path, table: str, trigger: str, function: str
) -> None:
    """Applying hardening to a database that lacks the table must fail loudly."""
    sql = path.read_text(encoding="utf-8")
    assert f"required v{version[1:]} append-only table is missing" in sql
    assert "RAISE EXCEPTION" in sql


@pytest.mark.parametrize(("version", "path", "table", "trigger", "function"), HARDENED)
def test_forward_layer_is_idempotent(
    version: str, path: Path, table: str, trigger: str, function: str
) -> None:
    """Re-running the lineage must not fail on an existing trigger."""
    sql = path.read_text(encoding="utf-8")
    assert f"DROP TRIGGER IF EXISTS {trigger}" in sql


@pytest.mark.parametrize("path", FROZEN)
def test_frozen_migrations_are_not_rewritten(path: Path) -> None:
    """The issue forbids editing them in place; hardening must live beside them."""
    sql = path.read_text(encoding="utf-8")
    assert "BEFORE TRUNCATE" not in sql


@pytest.mark.parametrize("path", FROZEN)
def test_frozen_migrations_still_match_their_packaged_copies(path: Path) -> None:
    packaged = ROOT / "app/platform_assets" / path.parent.name / "migrations" / path.name
    assert path.read_bytes() == packaged.read_bytes()


def test_ci_contract_applies_the_forward_layer() -> None:
    """A contract that never applies the hardening cannot observe it."""
    workflow = (
        ROOT / ".github/workflows/append-only-truncate-contract.yml"
    ).read_text(encoding="utf-8")
    for _version, path, _table, _trigger, _function in HARDENED:
        assert str(path.relative_to(ROOT)) in workflow


def test_ci_contract_checks_truncate_through_pg_trigger() -> None:
    """information_schema.triggers cannot see a TRUNCATE trigger, so it must not be used."""
    workflow = (
        ROOT / ".github/workflows/append-only-truncate-contract.yml"
    ).read_text(encoding="utf-8")
    assert "append-only TRUNCATE protection missing on %" in workflow
    assert "(t.tgtype & 32) = 32" in workflow
    for _version, _path, table, trigger, _function in HARDENED:
        assert f"('{table}', '{trigger}')" in workflow


DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")


@pytest.mark.skipif(not DSN, reason="TRUNCATE hardening tests require ASTRA_TEST_POSTGRES_DSN")
@pytest.mark.parametrize(("version", "path", "table", "trigger", "function"), HARDENED)
def test_truncate_is_rejected_on_a_real_database(
    version: str, path: Path, table: str, trigger: str, function: str
) -> None:
    """The point of the issue: the row-level guards let a table-level erase through."""
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(path.read_text(encoding="utf-8"))
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(f"TRUNCATE {table}")


@pytest.mark.skipif(not DSN, reason="TRUNCATE hardening tests require ASTRA_TEST_POSTGRES_DSN")
@pytest.mark.parametrize(("version", "path", "table", "trigger", "function"), HARDENED)
def test_applying_the_layer_twice_is_a_no_op(
    version: str, path: Path, table: str, trigger: str, function: str
) -> None:
    psycopg = pytest.importorskip("psycopg")
    sql = path.read_text(encoding="utf-8")
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql)
        connection.execute(sql)
        count = connection.execute(
            """SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
            WHERE c.relname = %s AND t.tgname = %s AND NOT t.tgisinternal""",
            (table, trigger),
        ).fetchone()[0]
    assert count == 1
