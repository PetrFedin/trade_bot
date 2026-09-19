"""Take a real backup, restore it in isolation, and report what actually came back.

F24 recorded that the V106 disaster-recovery model has no actuator underneath it: the
evidence objects are constructed in memory by the tests, so what is qualified is the
evaluator rather than the recoverability of the product. This runs the chain the issue
asks for and derives its evidence from the operation instead of from a caller's claim.

    read declared backup set -> pg_dump -> mutate the source -> restore into a fresh
    isolated database -> read it back -> compare table digests

The mutation between backup and restore is not incidental. Without it a drill can pass by
reading the source twice, and a restore that silently did nothing would look identical to
one that worked. Deleting rows after the backup is what forces the comparison to run
against the artifact.

The target is created fresh and named separately every run. It is never the source, and
the tool refuses to proceed if it were: restoring over the database being backed up is
the one mistake in this procedure that destroys what it was meant to protect.

Digest comparison, snapshot shape and divergence reporting live in
app/runtime/recovery_digest.py and hold no transport. This module owns the subprocesses
and the connections, and asserts nothing about whether the result is acceptable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # noqa: S404 - backup actuation is this module's purpose
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime.recovery_digest import (  # noqa: E402
    RecoveryComparison,
    StateSnapshot,
    build_snapshot,
    compare_snapshots,
)

# The declared backup set: canonical durable product state. Tables absent from a given
# database are skipped and reported, so the set can lead the migrations rather than
# silently shrink to whatever happens to exist.
BACKUP_SET = (
    "astra_oms_orders",
    "astra_oms_events",
    "astra_oms_outbox",
    "astra_order_mutations",
    "astra_order_mutation_events",
    "astra_order_mutation_outbox",
    "astra_risk_decisions",
    "astra_risk_chain_state",
    "astra_portfolio_events",
    "astra_portfolio_snapshots",
    "astra_portfolio_reconciliations",
    "astra_execution_facts",
    "astra_execution_fact_sources",
    "astra_execution_projection_events",
    "astra_execution_checkpoints",
    "astra_execution_checkpoint_events",
    "astra_financial_activity_facts",
    "astra_financial_activity_projection",
    "astra_financial_activity_recovery",
    "astra_operational_market_bars",
    "astra_operational_decision_tickets",
    "astra_paper_dispatch_control_events",
    "astra_paper_dispatch_control_state",
)


class DrillError(RuntimeError):
    """Raised when the drill cannot be run or the artifact cannot be trusted."""


@dataclass(frozen=True)
class BackupArtifact:
    """A real dump on disk, described by what was observed rather than declared."""

    path: Path
    size_bytes: int
    digest: str
    started_at: datetime
    completed_at: datetime
    source_lsn: str
    postgres_version: str

    @property
    def duration_seconds(self) -> float:
        return (self.completed_at - self.started_at).total_seconds()


def _dsn_with_database(dsn: str, database: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def _database_of(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/")


def _run(command: list[str], *, env: dict | None = None, timeout: int = 900) -> str:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as error:
        raise DrillError(f"{command[0]} is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise DrillError(f"{command[0]} timed out after {timeout}s") from error
    except subprocess.CalledProcessError as error:
        raise DrillError(f"{command[0]} failed: {error.stderr.strip()[:400]}") from error
    return completed.stdout


def client_major_version() -> int:
    """Major version of the pg_dump on PATH."""
    output = _run(["pg_dump", "--version"])
    for token in output.split():
        head = token.split(".")[0]
        if head.isdigit():
            return int(head)
    raise DrillError(f"could not read a version from {output.strip()!r}")


def server_major_version(dsn: str) -> int:
    with _connect(dsn) as connection:
        raw = str(connection.execute("SHOW server_version").fetchone()[0])
    return int(raw.split(".")[0])


def assert_client_is_compatible(dsn: str) -> tuple[int, int]:
    """Refuse a dump the target server will not be able to read back.

    A newer pg_dump emits settings an older server rejects - a 17 client against a 16
    server writes SET transaction_timeout, which 16 does not recognise - and the failure
    surfaces at restore time as a partial load rather than at backup time as an error.
    A drill whose artifact cannot be restored is worse than no drill, because it reports
    success for the half that ran.
    """
    client = client_major_version()
    server = server_major_version(dsn)
    if client > server:
        raise DrillError(
            f"pg_dump {client} is newer than the server ({server}); the artifact would "
            f"carry settings this server cannot restore. Use a client of version {server} "
            f"or older."
        )
    return client, server


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, autocommit=True)


def existing_tables(dsn: str) -> set[str]:
    with _connect(dsn) as connection:
        rows = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def read_snapshot(dsn: str, *, environment: str, tables: tuple[str, ...]) -> StateSnapshot:
    """Read the declared backup set out of one database."""
    import psycopg.rows

    present = existing_tables(dsn)
    collected: list[tuple[str, list[dict]]] = []
    with _connect(dsn) as connection:
        # Read the scalar before switching the row factory: dict rows are wanted for the
        # table contents, not for this.
        schema_version = str(
            connection.execute("SELECT current_setting('server_version')").fetchone()[0]
        )
        connection.row_factory = psycopg.rows.dict_row
        for table in tables:
            if table not in present:
                continue
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
            collected.append((table, list(rows)))
    if not collected:
        raise DrillError("no table from the declared backup set exists in this database")
    return build_snapshot(
        environment=environment,
        database=_database_of(dsn),
        schema_version=schema_version,
        captured_at=datetime.now(UTC),
        tables=collected,
    )


def take_backup(dsn: str, destination: Path) -> BackupArtifact:
    """Dump the database with pg_dump and describe the artifact that resulted."""
    import hashlib

    started = datetime.now(UTC)
    with _connect(dsn) as connection:
        lsn = str(connection.execute("SELECT pg_current_wal_lsn()").fetchone()[0])
        version = str(connection.execute("SELECT version()").fetchone()[0])
    _run(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(destination), dsn])
    completed = datetime.now(UTC)

    if not destination.exists() or destination.stat().st_size == 0:
        raise DrillError("pg_dump produced no artifact")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return BackupArtifact(
        path=destination,
        size_bytes=destination.stat().st_size,
        digest=digest,
        started_at=started,
        completed_at=completed,
        source_lsn=lsn,
        postgres_version=version.split(",")[0],
    )


def mutate_source(dsn: str, tables: tuple[str, ...]) -> int:
    """Delete rows from the source after the backup, proving the restore uses the artifact.

    Append-only tables refuse deletion by design, which is itself correct: those are the
    ones a restore must reproduce exactly. Whatever can be deleted is deleted, and the
    count is reported so a drill that mutated nothing cannot be mistaken for one that did.
    """
    removed = 0
    present = existing_tables(dsn)
    with _connect(dsn) as connection:
        for table in tables:
            if table not in present:
                continue
            try:
                cursor = connection.execute(f'DELETE FROM "{table}"')
                removed += cursor.rowcount or 0
            except Exception:  # noqa: BLE001 - append-only guards are expected here
                continue
    return removed


def restore(dsn_admin: str, target_database: str, artifact: Path) -> float:
    """Create a fresh isolated database and restore the artifact into it."""
    started = time.monotonic()
    with _connect(dsn_admin) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{target_database}"')
        connection.execute(f'CREATE DATABASE "{target_database}"')
    target_dsn = _dsn_with_database(dsn_admin, target_database)
    _run(["pg_restore", "--no-owner", "--no-acl", "--dbname", target_dsn, str(artifact)])
    return time.monotonic() - started


def run_drill(
    source_dsn: str,
    *,
    admin_dsn: str,
    target_database: str,
    environment: str = "drill",
    mutate: bool = True,
    tables: tuple[str, ...] = BACKUP_SET,
) -> dict:
    """Run the full chain and report what the restore reproduced."""
    if _database_of(source_dsn) == target_database:
        raise DrillError("the restore target must not be the database being backed up")

    client_version, server_version = assert_client_is_compatible(source_dsn)
    source_before = read_snapshot(source_dsn, environment=environment, tables=tables)
    with tempfile.TemporaryDirectory() as workspace:
        artifact_path = Path(workspace) / "astra-drill.dump"
        artifact = take_backup(source_dsn, artifact_path)
        deleted = mutate_source(source_dsn, tables) if mutate else 0
        restore_seconds = restore(admin_dsn, target_database, artifact_path)

    target_dsn = _dsn_with_database(admin_dsn, target_database)
    restored = read_snapshot(target_dsn, environment=f"{environment}-restored", tables=tables)
    comparison: RecoveryComparison = compare_snapshots(source_before, restored)

    return {
        "versions": {"pg_dump_major": client_version, "server_major": server_version},
        "backup": {
            "size_bytes": artifact.size_bytes,
            "object_digest": artifact.digest,
            "source_lsn": artifact.source_lsn,
            "postgres_version": artifact.postgres_version,
            "duration_seconds": round(artifact.duration_seconds, 3),
        },
        "mutation": {"applied": mutate, "rows_deleted": deleted},
        "restore": {
            "target_database": target_database,
            "duration_seconds": round(restore_seconds, 3),
        },
        "measured_rto_seconds": round(artifact.duration_seconds + restore_seconds, 3),
        "comparison": comparison.summary(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dsn", default=os.environ.get("ASTRA_TEST_POSTGRES_DSN"))
    parser.add_argument(
        "--target-database",
        default=f"astra_drill_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
    )
    parser.add_argument(
        "--no-mutate",
        action="store_true",
        help="skip deleting the source after backup; the drill then proves much less",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.source_dsn:
        print(
            "SOURCE_DSN_REQUIRED: pass --source-dsn or set ASTRA_TEST_POSTGRES_DSN",
            file=sys.stderr,
        )
        return 1

    admin_dsn = _dsn_with_database(args.source_dsn, "postgres")
    try:
        report = run_drill(
            args.source_dsn,
            admin_dsn=admin_dsn,
            target_database=args.target_database,
            mutate=not args.no_mutate,
        )
    except DrillError as error:
        print(f"DRILL_FAILED: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    comparison = report["comparison"]
    print(
        f"backup   : {report['backup']['size_bytes']} bytes in "
        f"{report['backup']['duration_seconds']}s at LSN {report['backup']['source_lsn']}"
    )
    print(f"mutation : {report['mutation']['rows_deleted']} rows deleted from the source")
    print(
        f"restore  : {report['restore']['target_database']} in "
        f"{report['restore']['duration_seconds']}s"
    )
    print(f"measured RTO: {report['measured_rto_seconds']}s")
    print(
        f"\ntables compared: {comparison['tables_compared']}   "
        f"schema matches: {comparison['schema_matches']}   "
        f"rows lost: {comparison['rows_lost']}"
    )
    if comparison["identical"]:
        print("RESULT: restored state is identical to the backup point")
        return 0
    print("RESULT: restored state diverged")
    for item in comparison["divergences"]:
        print(
            f"  {item['table']:<44}{item['kind']:<22}"
            f"source {item['source_rows']} restored {item['restored_rows']}"
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
