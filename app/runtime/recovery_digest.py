"""Derive recoverability evidence from a database rather than from a caller's claim.

The repository already models disaster recovery well: BackupManifestV106,
RestoreEvidenceV106 and DisasterRecoveryDrillV106 encode RPO, RTO, LSN, schema and
routing checks, and the V106 tests exercise that state machine thoroughly. F24 recorded
what is missing underneath it. Those tests construct evidence objects in memory, so what
is proven is the evaluator, not the recoverability of the product. Nothing in the shipped
application takes a backup, restores one, or compares what came back against what went in.

For a trading system a successful process restart is not disaster recovery. A restore
that silently drops one execution fact, one reservation or one HALT event can recreate
exposure that risk believes was closed, or reset a history that authorization depends on.
The difference has to be detectable, and detecting it means comparing content, not
counting rows.

This module supplies that comparison as pure functions. A table digest folds ordered rows
into one hash, a snapshot collects those digests across the declared backup set, and a
comparison names exactly which tables diverged and how. Reading a database and running
pg_dump are operational concerns and live in tools/, the way every other transport in this
codebase does, so what remains here can be exercised from recorded rows.

Nothing here decides that a restore is acceptable. It states what differs, which is the
input a qualification decision needs and the one thing an optimistic boolean cannot give.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


class RecoveryDigestError(ValueError):
    """Raised when a snapshot is malformed or two snapshots cannot be compared."""


def _canonical(value: object) -> object:
    """Render a database value so equal content always produces equal bytes."""
    if isinstance(value, Decimal):
        # 1.10 and 1.1 are the same quantity and must not look like a divergence.
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise RecoveryDigestError("timestamps must be timezone-aware to be compared")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, bytes | bytearray):
        return value.hex()
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def row_digest(row: Mapping[str, object]) -> str:
    """Digest one row, independent of column order."""
    material = {str(key): _canonical(value) for key, value in sorted(row.items())}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def table_digest(rows: Iterable[Mapping[str, object]]) -> tuple[str, int]:
    """Fold rows into one digest and a count, independent of the order they arrived.

    Ordering by row digest rather than trusting the query's order means a restore that
    returns the same content under a different physical layout is not reported as a
    divergence, while a single changed byte in any row is.
    """
    digests = sorted(row_digest(row) for row in rows)
    folded = hashlib.sha256("".join(digests).encode("utf-8")).hexdigest()
    return folded, len(digests)


@dataclass(frozen=True)
class TableSnapshot:
    """What one table contained at one moment."""

    table: str
    digest: str
    row_count: int

    def validate(self) -> None:
        if not self.table or self.table != self.table.lower():
            raise RecoveryDigestError("table must be a non-empty lowercase identifier")
        if len(self.digest) != 64:
            raise RecoveryDigestError("digest must be a sha256 hex digest")
        if self.row_count < 0:
            raise RecoveryDigestError("row_count must not be negative")


@dataclass(frozen=True)
class StateSnapshot:
    """The declared backup set as it stood, with the identity of the system it came from."""

    environment: str
    database: str
    schema_version: str
    captured_at: datetime
    tables: tuple[TableSnapshot, ...]

    def validate(self) -> None:
        for name in ("environment", "database", "schema_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RecoveryDigestError(f"{name} must be a non-empty string")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise RecoveryDigestError("captured_at must be timezone-aware")
        if not self.tables:
            raise RecoveryDigestError("a snapshot must cover at least one table")
        names = [table.table for table in self.tables]
        if len(names) != len(set(names)):
            raise RecoveryDigestError("a table may appear only once in a snapshot")
        for table in self.tables:
            table.validate()

    @property
    def total_rows(self) -> int:
        return sum(table.row_count for table in self.tables)

    @property
    def digest(self) -> str:
        """One digest over the whole declared set, for the recovery-point comparison."""
        material = [
            {"table": table.table, "digest": table.digest, "rows": table.row_count}
            for table in sorted(self.tables, key=lambda item: item.table)
        ]
        encoded = json.dumps(
            {"schema_version": self.schema_version, "tables": material},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TableDivergence:
    """One table that did not come back the way it went in."""

    table: str
    source_digest: str | None
    restored_digest: str | None
    source_rows: int | None
    restored_rows: int | None

    @property
    def kind(self) -> str:
        if self.source_digest is None:
            return "ONLY_IN_RESTORED"
        if self.restored_digest is None:
            return "MISSING_FROM_RESTORED"
        if self.source_rows != self.restored_rows:
            return "ROW_COUNT_DIFFERS"
        return "CONTENT_DIFFERS"


@dataclass(frozen=True)
class RecoveryComparison:
    """What a restore actually reproduced, stated table by table."""

    source: StateSnapshot
    restored: StateSnapshot
    divergences: tuple[TableDivergence, ...]

    @property
    def identical(self) -> bool:
        return not self.divergences

    @property
    def schema_matches(self) -> bool:
        return self.source.schema_version == self.restored.schema_version

    @property
    def rows_lost(self) -> int:
        """Rows present in the source that the restore did not reproduce."""
        return sum(
            max(0, (item.source_rows or 0) - (item.restored_rows or 0))
            for item in self.divergences
        )

    def summary(self) -> dict:
        return {
            "identical": self.identical,
            "schema_matches": self.schema_matches,
            "source_digest": self.source.digest,
            "restored_digest": self.restored.digest,
            "tables_compared": len(self.source.tables),
            "rows_lost": self.rows_lost,
            "divergences": [
                {
                    "table": item.table,
                    "kind": item.kind,
                    "source_rows": item.source_rows,
                    "restored_rows": item.restored_rows,
                }
                for item in self.divergences
            ],
        }


def compare_snapshots(source: StateSnapshot, restored: StateSnapshot) -> RecoveryComparison:
    """Report every table that did not survive the round trip unchanged."""
    source.validate()
    restored.validate()

    source_tables = {table.table: table for table in source.tables}
    restored_tables = {table.table: table for table in restored.tables}
    divergences: list[TableDivergence] = []

    for name in sorted(set(source_tables) | set(restored_tables)):
        left = source_tables.get(name)
        right = restored_tables.get(name)
        if left is not None and right is not None and left.digest == right.digest:
            continue
        divergences.append(
            TableDivergence(
                table=name,
                source_digest=None if left is None else left.digest,
                restored_digest=None if right is None else right.digest,
                source_rows=None if left is None else left.row_count,
                restored_rows=None if right is None else right.row_count,
            )
        )
    return RecoveryComparison(source=source, restored=restored, divergences=tuple(divergences))


def build_snapshot(
    *,
    environment: str,
    database: str,
    schema_version: str,
    captured_at: datetime,
    tables: Sequence[tuple[str, Iterable[Mapping[str, object]]]],
) -> StateSnapshot:
    """Fold rows per table into a validated snapshot of the declared backup set."""
    # astimezone() on a naive value silently assumes local time, which would move a
    # recovery point by the machine's offset. Refuse it where it enters.
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise RecoveryDigestError("captured_at must be timezone-aware")
    entries = []
    for name, rows in tables:
        digest, count = table_digest(rows)
        entries.append(TableSnapshot(table=name, digest=digest, row_count=count))
    snapshot = StateSnapshot(
        environment=environment,
        database=database,
        schema_version=schema_version,
        captured_at=captured_at.astimezone(UTC),
        tables=tuple(sorted(entries, key=lambda item: item.table)),
    )
    snapshot.validate()
    return snapshot
