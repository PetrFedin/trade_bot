from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "migrations/v107/001_production_rollout_actuator.sql"
PACKAGED = ROOT / "app/platform_assets/v107/migrations/001_production_rollout_actuator.sql"
_CREATE_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+([a-z0-9_]+)\s*\(", re.IGNORECASE)
_CONSTRAINT_PREFIXES = {"check", "constraint", "primary", "foreign", "unique", "exclude"}


def _table_bodies(sql: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for match in _CREATE_TABLE.finditer(sql):
        depth = 1
        cursor = match.end()
        while cursor < len(sql) and depth:
            char = sql[cursor]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            cursor += 1
        assert depth == 0, f"unterminated CREATE TABLE for {match.group(1)}"
        bodies[match.group(1)] = sql[match.end() : cursor - 1]
    return bodies


def _top_level_columns(body: str) -> list[str]:
    segments: list[str] = []
    start = 0
    depth = 0
    in_single_quote = False
    cursor = 0
    while cursor < len(body):
        char = body[cursor]
        if char == "'":
            if in_single_quote and cursor + 1 < len(body) and body[cursor + 1] == "'":
                cursor += 2
                continue
            in_single_quote = not in_single_quote
        elif not in_single_quote:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                segments.append(body[start:cursor])
                start = cursor + 1
        cursor += 1
    segments.append(body[start:])

    columns: list[str] = []
    for segment in segments:
        tokens = segment.strip().split()
        if not tokens or tokens[0].lower() in _CONSTRAINT_PREFIXES:
            continue
        columns.append(tokens[0].strip('"').lower())
    return columns


def test_packaged_migration_is_byte_identical() -> None:
    assert CANONICAL.read_bytes() == PACKAGED.read_bytes()


def test_create_tables_have_no_duplicate_columns() -> None:
    for table, body in _table_bodies(CANONICAL.read_text(encoding="utf-8")).items():
        columns = _top_level_columns(body)
        assert len(columns) == len(set(columns)), f"duplicate column in {table}: {columns}"


def test_execution_and_fence_schema_match_repository_contract() -> None:
    bodies = _table_bodies(CANONICAL.read_text(encoding="utf-8"))
    fence = set(_top_level_columns(bodies["astra_rollout_fence_v107"]))
    execution = set(_top_level_columns(bodies["astra_rollout_execution_v107"]))
    assert {"deployment_uid", "fencing_token", "command_id", "updated_at"} <= fence
    assert {"deployment_uid", "fencing_token", "command_id", "command_json", "state"} <= execution
