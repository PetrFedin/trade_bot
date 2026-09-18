from __future__ import annotations

from pathlib import Path

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
