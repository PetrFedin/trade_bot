from __future__ import annotations

import sys
from types import SimpleNamespace

import tools.bootstrap_db as bootstrap


def test_lineage_is_version_ordered_and_excludes_product_migrations() -> None:
    migrations = bootstrap.lineage()
    assert migrations
    relative = [path.relative_to(bootstrap.ROOT) for path in migrations]
    assert relative == sorted(
        relative,
        key=lambda path: (
            int(path.parts[1][1:]),
            str(path),
        ),
    )
    assert all(path.parts[0] == "migrations" for path in relative)
    assert all(path.parts[1].startswith("v") for path in relative)
    assert all("product" not in path.parts for path in relative)


def test_verify_only_checks_repository_lineage_without_database() -> None:
    assert bootstrap.main(["--verify-only"]) == 0


def test_destructive_reset_accepts_only_local_qualification_hosts() -> None:
    for dsn in (
        "postgresql://astra:astra@127.0.0.1:5433/astra",
        "postgresql://astra:astra@localhost:5433/astra",
        "postgresql://astra:astra@postgres:5432/astra",
        "postgresql://astra:astra@[::1]:5432/astra",
    ):
        assert bootstrap.local_reset_dsn(dsn)

    for dsn in (
        "postgresql://astra:astra@db.internal.example:5432/astra",
        "postgresql://astra:astra@10.20.30.40:5432/astra",
        "postgresql:///astra",
        "dbname=astra host=localhost",
        "",
    ):
        assert not bootstrap.local_reset_dsn(dsn)


def test_remote_reset_is_rejected_before_database_connection(monkeypatch) -> None:
    called = False

    def forbidden_reset(dsn: str) -> list[str]:
        nonlocal called
        called = True
        raise AssertionError(dsn)

    monkeypatch.setattr(bootstrap, "reset", forbidden_reset)
    result = bootstrap.main(
        [
            "--dsn",
            "postgresql://astra:secret@db.internal.example:5432/astra",
            "--reset",
        ]
    )
    assert result == 2
    assert called is False


def test_every_packaged_platform_copy_matches_source() -> None:
    drift = bootstrap.verify(bootstrap.lineage())
    assert drift == []


def test_packaging_coverage_does_not_count_missing_copies_as_verified() -> None:
    migrations = bootstrap.lineage()
    packaged_count, missing = bootstrap.packaging_coverage(migrations)
    assert packaged_count + len(missing) == len(migrations)
    assert all(bootstrap.packaged_copy(path) is None for path in missing)
    assert packaged_count == sum(
        bootstrap.packaged_copy(path) is not None for path in migrations
    )


def test_verify_reports_drift_for_existing_packaged_copy(tmp_path, monkeypatch) -> None:
    root = tmp_path
    migration = root / "migrations" / "v999" / "001.sql"
    packaged = root / "app" / "platform_assets" / "v999" / "migrations" / "001.sql"
    migration.parent.mkdir(parents=True)
    packaged.parent.mkdir(parents=True)
    migration.write_text("SELECT 1;")
    packaged.write_text("SELECT 2;")

    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "PACKAGED", root / "app" / "platform_assets")

    drift = bootstrap.verify([migration])
    assert len(drift) == 1
    assert drift[0].startswith("MIGRATION_DRIFT:")


def test_main_fails_closed_for_empty_lineage(monkeypatch, capsys) -> None:
    monkeypatch.setattr(bootstrap, "lineage", lambda: [])
    assert bootstrap.main(["--verify-only"]) == 1
    assert "NO_MIGRATIONS_FOUND" in capsys.readouterr().err


def test_main_fails_closed_for_packaged_drift(monkeypatch, capsys, tmp_path) -> None:
    migration = tmp_path / "migrations" / "v999" / "001.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("SELECT 1;")
    monkeypatch.setattr(bootstrap, "lineage", lambda: [migration])
    monkeypatch.setattr(bootstrap, "verify", lambda _migrations: ["MIGRATION_DRIFT: test"])
    assert bootstrap.main(["--verify-only"]) == 1
    assert "MIGRATION_DRIFT: test" in capsys.readouterr().err


def test_main_requires_dsn_when_apply_is_requested(monkeypatch, capsys, tmp_path) -> None:
    migration = tmp_path / "migrations" / "v999" / "001.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("SELECT 1;")
    monkeypatch.setattr(bootstrap, "lineage", lambda: [migration])
    monkeypatch.setattr(bootstrap, "verify", lambda _migrations: [])
    monkeypatch.setattr(bootstrap, "packaging_coverage", lambda _migrations: (1, ()))
    monkeypatch.delenv("ASTRA_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("ASTRA_TEST_POSTGRES_DSN", raising=False)

    assert bootstrap.main([]) == 1
    assert "DSN_REQUIRED" in capsys.readouterr().err


def test_reset_drops_only_returned_non_system_schemas(monkeypatch) -> None:
    statements: list[str] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement: str):
            statements.append(statement)
            if statement.startswith("SELECT nspname"):
                return SimpleNamespace(
                    fetchall=lambda: [("public",), ("astra_v999",)]
                )
            return None

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda *_args, **_kwargs: Connection()),
    )

    dropped = bootstrap.reset("postgresql://astra@localhost/astra")
    assert dropped == ["astra_v999", "public"]
    assert 'DROP SCHEMA IF EXISTS "public" CASCADE' in statements
    assert 'DROP SCHEMA IF EXISTS "astra_v999" CASCADE' in statements
    assert "CREATE SCHEMA IF NOT EXISTS public" in statements


def test_apply_runs_each_migration_for_every_requested_pass(tmp_path, monkeypatch) -> None:
    root = tmp_path
    migration = root / "migrations" / "v999" / "001.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("SELECT 999;")
    executed: list[str] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement: str):
            executed.append(statement)

    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda *_args, **_kwargs: Connection()),
    )

    bootstrap.apply("postgresql://astra@localhost/astra", [migration], passes=3)
    assert executed == ["SELECT 999;"] * 3


def test_apply_reports_migration_provenance_on_failure(tmp_path, monkeypatch) -> None:
    root = tmp_path
    migration = root / "migrations" / "v999" / "001.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("BROKEN SQL")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement: str):
            raise RuntimeError("database rejected statement")

    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda *_args, **_kwargs: Connection()),
    )

    try:
        bootstrap.apply("postgresql://astra@localhost/astra", [migration], passes=1)
    except SystemExit as error:
        message = str(error)
    else:
        raise AssertionError("expected migration failure")

    assert "MIGRATION_FAILED pass=1" in message
    assert "migrations/v999/001.sql" in message
    assert "database rejected statement" in message
