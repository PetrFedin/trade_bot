from __future__ import annotations

from datetime import datetime
from typing import Any

from app.execution.bybit_demo_v121_control_records import (
    BybitDemoControlDecisionV121,
    BybitDemoControlEventV121,
    BybitDemoControlModeV121,
    decision_from_control_event_v121,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_TABLE = "astra_bybit_demo_control_event_v121"
_APPEND_TRIGGER = "astra_bybit_demo_control_append_only_v121"
_TRUNCATE_TRIGGER = "astra_bybit_demo_control_no_truncate_v121"
_MUTATION_FUNCTION = "astra_reject_bybit_demo_control_mutation_v121"
_SELECT_LATEST_SQL = """SELECT event_id, event_kind, operator_id, reason,
preflight_status, preflight_record_sha256, preflight_canonical_record,
preflight_observed_at, armed_until, created_at,
immutable_record, order_submission_supported, live_mainnet_order_routing_allowed
FROM astra_bybit_demo_control_event_v121
ORDER BY event_seq DESC
LIMIT 1"""
_SELECT_EVENT_SQL = """SELECT event_id, event_kind, operator_id, reason,
preflight_status, preflight_record_sha256, preflight_canonical_record,
preflight_observed_at, armed_until, created_at,
immutable_record, order_submission_supported, live_mainnet_order_routing_allowed
FROM astra_bybit_demo_control_event_v121
WHERE event_id=%s"""
_INSERT_EVENT_SQL = """INSERT INTO astra_bybit_demo_control_event_v121(
event_id,
event_kind,
operator_id,
reason,
preflight_status,
preflight_record_sha256,
preflight_canonical_record,
preflight_observed_at,
armed_until,
created_at,
immutable_record,
order_submission_supported,
live_mainnet_order_routing_allowed
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""


class PostgresBybitDemoControlJournalReaderV121:
    """Read-only v121 journal view with fail-closed decision rehydration."""

    automatic_migration_allowed = False
    runtime_ddl_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    broker_network_supported = False
    market_data_reads_supported = False
    strategy_decisions_supported = False
    live_mainnet_order_routing_allowed = False

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Bybit Demo v121 control reader PostgreSQL DSN is required")
        self._dsn = dsn

    def read_decision(self, *, now: datetime) -> BybitDemoControlDecisionV121:
        _require_postgres_dependency()
        with psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=False,
        ) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    readiness = _journal_readiness(cursor)
                    if readiness is not None:
                        return _halted(readiness)
                    row = cursor.execute(_SELECT_LATEST_SQL).fetchone()
        if row is None:
            return decision_from_control_event_v121(None, now=now)
        try:
            event = BybitDemoControlEventV121.from_db_row(row)
        except ValueError:
            return _halted("DEMO_CONTROL_EVENT_INVALID")
        return decision_from_control_event_v121(event, now=now)

    def load_latest_event(self) -> BybitDemoControlEventV121 | None:
        _require_postgres_dependency()
        with psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=False,
        ) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    readiness = _journal_readiness(cursor)
                    if readiness is not None:
                        raise RuntimeError(
                            "Bybit Demo v121 control journal is not ready:"
                            f"{readiness}"
                        )
                    row = cursor.execute(_SELECT_LATEST_SQL).fetchone()
        return None if row is None else BybitDemoControlEventV121.from_db_row(row)

    def load_event(self, *, event_id: str) -> BybitDemoControlEventV121:
        if not _is_sha256(event_id):
            raise ValueError("Bybit Demo v121 control event_id is invalid")
        _require_postgres_dependency()
        with psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=False,
        ) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    readiness = _journal_readiness(cursor)
                    if readiness is not None:
                        raise RuntimeError(
                            "Bybit Demo v121 control journal is not ready:"
                            f"{readiness}"
                        )
                    row = cursor.execute(_SELECT_EVENT_SQL, (event_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("Bybit Demo v121 control event does not exist")
        return BybitDemoControlEventV121.from_db_row(row)


class PostgresBybitDemoControlJournalWriterV121:
    """Append-only v121 journal writer; no broker, order or migration capability."""

    automatic_migration_allowed = False
    runtime_ddl_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    broker_network_supported = False
    market_data_reads_supported = False
    strategy_decisions_supported = False
    live_mainnet_order_routing_allowed = False

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Bybit Demo v121 control writer PostgreSQL DSN is required")
        self._dsn = dsn

    def append(self, event: BybitDemoControlEventV121) -> BybitDemoControlEventV121:
        if not isinstance(event, BybitDemoControlEventV121):
            raise TypeError("Bybit Demo v121 control writer requires a typed control event")
        _require_postgres_dependency()
        with psycopg.connect(
            self._dsn,
            row_factory=dict_row,
            autocommit=False,
        ) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    readiness = _journal_readiness(cursor)
                    if readiness is not None:
                        raise RuntimeError(
                            f"Bybit Demo v121 control journal is not ready:{readiness}"
                        )
                    try:
                        cursor.execute(_INSERT_EVENT_SQL, event.to_db_values())
                    except psycopg.errors.UniqueViolation as exc:
                        raise FileExistsError(
                            "Bybit Demo v121 control event already exists"
                        ) from exc
        return event


def _journal_readiness(cursor: Any) -> str | None:
    relation = cursor.execute("SELECT to_regclass(%s) AS relation", (_TABLE,)).fetchone()
    if relation is None:
        return "DEMO_CONTROL_SCHEMA_NOT_READY"
    relation_value = relation["relation"] if isinstance(relation, dict) else relation[0]
    if relation_value is None:
        return "DEMO_CONTROL_SCHEMA_NOT_READY"

    requirements = (
        (_APPEND_TRIGGER, False, "DEMO_CONTROL_APPEND_ONLY_TRIGGER_NOT_READY"),
        (_TRUNCATE_TRIGGER, True, "DEMO_CONTROL_TRUNCATE_TRIGGER_NOT_READY"),
    )
    for trigger_name, truncate, reason in requirements:
        row = cursor.execute(
            """SELECT count(*) AS trigger_count
               FROM pg_trigger t
               JOIN pg_class c ON c.oid=t.tgrelid
               JOIN pg_namespace n ON n.oid=c.relnamespace
               JOIN pg_proc p ON p.oid=t.tgfoid
               JOIN pg_namespace pn ON pn.oid=p.pronamespace
               WHERE n.nspname='public'
                 AND c.relname=%s
                 AND t.tgname=%s
                 AND NOT t.tgisinternal
                 AND t.tgenabled IN ('O','A')
                 AND pn.nspname='public'
                 AND p.proname=%s
                 AND ((%s AND (t.tgtype & 32)=32 AND (t.tgtype & 1)=0)
                      OR (NOT %s AND (t.tgtype & 8)=8 AND (t.tgtype & 16)=16
                          AND (t.tgtype & 1)=1))""",
            (_TABLE, trigger_name, _MUTATION_FUNCTION, truncate, truncate),
        ).fetchone()
        count = row["trigger_count"] if isinstance(row, dict) else row[0]
        if count != 1:
            return reason
    return None


def _halted(reason: str) -> BybitDemoControlDecisionV121:
    return BybitDemoControlDecisionV121(
        mode=BybitDemoControlModeV121.HALTED,
        reasons=(reason,),
        new_entry_allowed=False,
        latest_event_id=None,
        latest_event_kind=None,
        armed_until=None,
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_postgres_dependency() -> None:
    if psycopg is None or dict_row is None:
        raise RuntimeError("PostgreSQL dependency is unavailable")


__all__ = [
    "PostgresBybitDemoControlJournalReaderV121",
    "PostgresBybitDemoControlJournalWriterV121",
]
