"""Apply platform migrations in lineage order and verify their packaged copies.

Every workflow that needs a database currently hardcodes its own ordered list of
migration files. This module makes that list derivable from the repository layout
so a checkout can bootstrap a database without copying a CI recipe by hand.

Two invariants are enforced, both already relied on by CI:

* a migration under ``migrations/vNNN/`` must be byte-identical to its packaged
  copy under ``app/platform_assets/vNNN/migrations/`` when that copy exists;
* applying the whole lineage twice must be a no-op the second time.

Product-level migrations under ``migrations/product/`` are intentionally not applied
here: they are owned by ``build_postgres_product(..., migrate=True)``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
PACKAGED = ROOT / "app" / "platform_assets"
VERSION = re.compile(r"^v(\d+)$")


def lineage() -> list[Path]:
    """Return every platform migration, ordered by version then filename."""
    versions: list[tuple[int, Path]] = []
    for entry in MIGRATIONS.iterdir():
        if not entry.is_dir():
            continue
        match = VERSION.match(entry.name)
        if match:
            versions.append((int(match.group(1)), entry))
    ordered: list[Path] = []
    for _, directory in sorted(versions):
        ordered.extend(sorted(directory.glob("*.sql")))
    return ordered


def packaged_copy(migration: Path) -> Path | None:
    """Return the packaged copy of a migration, when the release ships one."""
    candidate = PACKAGED / migration.parent.name / "migrations" / migration.name
    return candidate if candidate.exists() else None


def verify(migrations: list[Path]) -> list[str]:
    """Return a drift report for migrations whose packaged copy differs."""
    drift: list[str] = []
    for migration in migrations:
        copy = packaged_copy(migration)
        if copy is None:
            continue
        if migration.read_bytes() != copy.read_bytes():
            drift.append(
                f"MIGRATION_DRIFT: {migration.relative_to(ROOT)} != {copy.relative_to(ROOT)}"
            )
    return drift


_LOCAL_RESET_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "postgres"})


def local_reset_dsn(dsn: str) -> bool:
    """Return whether destructive reset is confined to the local qualification zone."""
    try:
        parsed = urlparse(dsn)
    except ValueError:
        return False
    return (
        parsed.scheme in {"postgres", "postgresql"}
        and parsed.hostname in _LOCAL_RESET_HOSTS
    )


def reset(dsn: str) -> list[str]:
    """Drop every non-system schema, returning the names dropped.

    The migrations do not live in "public": they create versioned schemas - astra_v99
    through astra_v121 and astra_platform - and a reset that only clears "public"
    leaves all of them standing. State then accumulates across runs in schemas nobody
    looked at, and the fleet-deployment tests fail on the second pass while passing in
    isolation, which reads as flakiness rather than an uncleaned database.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        rows = connection.execute(
            """SELECT nspname FROM pg_namespace
            WHERE nspname NOT IN ('pg_catalog', 'information_schema')
              AND nspname NOT LIKE 'pg\\_temp%' AND nspname NOT LIKE 'pg\\_toast%'"""
        ).fetchall()
        names = [str(row[0]) for row in rows]
        for name in names:
            connection.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        connection.execute("CREATE SCHEMA IF NOT EXISTS public")
    return sorted(names)


def apply(dsn: str, migrations: list[Path], *, passes: int = 2) -> None:
    """Apply the lineage ``passes`` times; a second pass proves idempotency."""
    import psycopg

    for attempt in range(1, passes + 1):
        with psycopg.connect(dsn, autocommit=True) as connection:
            for migration in migrations:
                statement = migration.read_text()
                try:
                    connection.execute(statement)
                except Exception as error:  # noqa: BLE001 - reported with provenance
                    raise SystemExit(
                        f"MIGRATION_FAILED pass={attempt} "
                        f"file={migration.relative_to(ROOT)}: {error}"
                    ) from error
        print(f"pass {attempt}/{passes}: applied {len(migrations)} migrations")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("ASTRA_POSTGRES_DSN") or os.environ.get("ASTRA_TEST_POSTGRES_DSN"),
        help="PostgreSQL DSN (default: ASTRA_POSTGRES_DSN, then ASTRA_TEST_POSTGRES_DSN)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop every non-system schema before applying; destroys all durable state",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="check packaged copies for drift and list the lineage without connecting",
    )
    parser.add_argument(
        "--passes",
        type=int,
        default=2,
        help="how many times to apply the lineage (default 2, proving idempotency)",
    )
    args = parser.parse_args(argv)

    migrations = lineage()
    if not migrations:
        print("NO_MIGRATIONS_FOUND", file=sys.stderr)
        return 1

    drift = verify(migrations)
    if drift:
        for line in drift:
            print(line, file=sys.stderr)
        return 1
    print(f"verified {len(migrations)} migrations against their packaged copies")

    if args.verify_only:
        for migration in migrations:
            print(f"  {migration.relative_to(ROOT)}")
        return 0

    if not args.dsn:
        print(
            "DSN_REQUIRED: pass --dsn or set ASTRA_POSTGRES_DSN",
            file=sys.stderr,
        )
        return 1

    if args.reset:
        if not local_reset_dsn(args.dsn):
            print(
                "RESET_REFUSED: --reset is restricted to the local qualification zone",
                file=sys.stderr,
            )
            return 2
        dropped = reset(args.dsn)
        print(f"dropped {len(dropped)} schema(s): {', '.join(dropped)}")

    apply(args.dsn, migrations, passes=args.passes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
