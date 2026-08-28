from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_connected_preflight import (
    PostgresBybitDemoOperationalStateReader,
)
from app.execution.bybit_demo_operational_database_identity import (
    PostgresBybitDemoOperationalDatabaseIdentityReader,
)

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None

_CONFIRMATION_PHRASES = frozenset(
    {
        "APPLY_BYBIT_DEMO_V119_V123",
        "APPLY_BYBIT_DEMO_V119_V124",
    }
)
_ADVISORY_LOCK_KEY = 119124
_CONTROL_RELATION = "astra_bybit_demo_control_event_v121"
_CONTROL_TRIGGERS = (
    "astra_bybit_demo_control_append_only_v121",
    "astra_bybit_demo_control_no_truncate_v123",
)
_SESSION_RISK_RELATIONS = (
    "astra_bybit_demo_session_risk_v122",
    "astra_bybit_demo_session_trade_outcome_v122",
)
_SESSION_RISK_TRIGGERS = (
    "astra_bybit_demo_session_risk_guard_v122",
    "astra_bybit_demo_session_risk_no_truncate_v122",
    "astra_bybit_demo_session_outcome_append_only_v122",
    "astra_bybit_demo_session_outcome_no_truncate_v122",
)
_RECOVERY_RELATION = "astra_bybit_demo_runtime_lease_recovery_v123"
_RECOVERY_TRIGGERS = (
    "astra_bybit_demo_runtime_lease_recovery_append_only_v123",
    "astra_bybit_demo_runtime_lease_recovery_no_truncate_v123",
)
_IDENTITY_RELATION = "astra_bybit_demo_operational_identity_v124"
_IDENTITY_TRIGGERS = (
    "astra_bybit_demo_operational_identity_immutable_v124",
    "astra_bybit_demo_operational_identity_no_truncate_v124",
)
_MIGRATIONS = (
    ("v119", Path("migrations/v119/001_bybit_demo_durable_runtime.sql")),
    ("v120", Path("migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql")),
    ("v121", Path("migrations/v121/001_bybit_demo_control_plane.sql")),
    ("v122", Path("migrations/v122/001_bybit_demo_postgres_session_risk.sql")),
    ("v123", Path("migrations/v123/001_bybit_demo_runtime_lease_recovery.sql")),
    ("v124", Path("migrations/v124/001_bybit_demo_operational_database_identity.sql")),
)


class BybitDemoPostgresBootstrapStatus(StrEnum):
    VERIFIED_READY = "VERIFIED_READY"
    SCHEMA_NOT_READY = "SCHEMA_NOT_READY"
    APPLIED_AND_VERIFIED = "APPLIED_AND_VERIFIED"


@dataclass(frozen=True)
class BybitDemoPostgresMigrationFingerprint:
    version: str
    path: str
    sha256: str


@dataclass(frozen=True)
class BybitDemoPostgresBootstrapResult:
    status: BybitDemoPostgresBootstrapStatus
    schema_mutation_performed: bool
    required_relations_present: bool
    append_only_triggers_present: bool
    migration_fingerprints: tuple[BybitDemoPostgresMigrationFingerprint, ...]
    logical_database_identity_verified: bool = False
    database_identity_exposed: bool = False
    bybit_credentials_required: bool = False
    bybit_order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    @property
    def passed(self) -> bool:
        return self.status in {
            BybitDemoPostgresBootstrapStatus.VERIFIED_READY,
            BybitDemoPostgresBootstrapStatus.APPLIED_AND_VERIFIED,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V4",
            "status": self.status.value,
            "passed": self.passed,
            "schema_mutation_performed": self.schema_mutation_performed,
            "required_relations_present": self.required_relations_present,
            "append_only_triggers_present": self.append_only_triggers_present,
            "migration_fingerprints": [
                {
                    "version": item.version,
                    "path": item.path,
                    "sha256": item.sha256,
                }
                for item in self.migration_fingerprints
            ],
            "logical_database_identity_verified": self.logical_database_identity_verified,
            "database_identity_exposed": self.database_identity_exposed,
            "bybit_credentials_required": self.bybit_credentials_required,
            "bybit_order_writes_supported": self.bybit_order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


def verify_bybit_demo_postgres_schema(dsn: str) -> BybitDemoPostgresBootstrapResult:
    """Verify the durable Demo runtime/audit/control/risk/recovery/identity schema read-only."""

    fingerprints = _migration_fingerprints()
    state = PostgresBybitDemoOperationalStateReader(dsn).read_state()
    control_relation, control_triggers = _verify_control_schema(dsn)
    risk_relations, risk_triggers = _verify_session_risk_schema(dsn)
    recovery_relation, recovery_triggers = _verify_recovery_schema(dsn)
    identity_relation, identity_triggers, identity_verified = _verify_identity_schema(dsn)
    relations_ready = (
        state.required_relations_present
        and control_relation
        and risk_relations
        and recovery_relation
        and identity_relation
    )
    triggers_ready = (
        state.append_only_triggers_present
        and control_triggers
        and risk_triggers
        and recovery_triggers
        and identity_triggers
    )
    ready = relations_ready and triggers_ready and identity_verified
    return BybitDemoPostgresBootstrapResult(
        status=(
            BybitDemoPostgresBootstrapStatus.VERIFIED_READY
            if ready
            else BybitDemoPostgresBootstrapStatus.SCHEMA_NOT_READY
        ),
        schema_mutation_performed=False,
        required_relations_present=relations_ready,
        append_only_triggers_present=triggers_ready,
        migration_fingerprints=fingerprints,
        logical_database_identity_verified=identity_verified,
    )


def apply_bybit_demo_postgres_bootstrap(
    dsn: str,
    *,
    confirmation_phrase: str,
) -> BybitDemoPostgresBootstrapResult:
    """Apply exactly v119 through v124 under a session advisory lock and verify."""

    if confirmation_phrase not in _CONFIRMATION_PHRASES:
        raise ValueError("Bybit Demo PostgreSQL bootstrap confirmation phrase is invalid")
    if not dsn.strip():
        raise ValueError("Bybit Demo PostgreSQL bootstrap DSN is required")
    if psycopg is None:
        raise RuntimeError("PostgreSQL dependency is unavailable")

    fingerprints = _migration_fingerprints()
    with psycopg.connect(dsn, autocommit=True) as connection:
        acquired = connection.execute(
            "SELECT pg_try_advisory_lock(%s)",
            (_ADVISORY_LOCK_KEY,),
        ).fetchone()
        if acquired is None or acquired[0] is not True:
            raise RuntimeError("Bybit Demo PostgreSQL bootstrap advisory lock is busy")
        try:
            try:
                for _version, path in _MIGRATIONS:
                    connection.execute(path.read_text(encoding="utf-8"))
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.execute(
                "SELECT pg_advisory_unlock(%s)",
                (_ADVISORY_LOCK_KEY,),
            )

    verified = verify_bybit_demo_postgres_schema(dsn)
    if not verified.required_relations_present:
        raise RuntimeError("Bybit Demo PostgreSQL bootstrap relations verification failed")
    if not verified.append_only_triggers_present:
        raise RuntimeError("Bybit Demo PostgreSQL bootstrap append-only verification failed")
    if not verified.logical_database_identity_verified:
        raise RuntimeError("Bybit Demo PostgreSQL logical database identity verification failed")
    return BybitDemoPostgresBootstrapResult(
        status=BybitDemoPostgresBootstrapStatus.APPLIED_AND_VERIFIED,
        schema_mutation_performed=True,
        required_relations_present=True,
        append_only_triggers_present=True,
        migration_fingerprints=fingerprints,
        logical_database_identity_verified=True,
    )


def _verify_control_schema(dsn: str) -> tuple[bool, bool]:
    if psycopg is None:
        raise RuntimeError("PostgreSQL dependency is unavailable")
    with psycopg.connect(dsn, autocommit=False) as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT to_regclass(%s)", (_CONTROL_RELATION,))
                relation = cursor.fetchone()
                relation_ready = relation is not None and relation[0] is not None
                if not relation_ready:
                    return False, False
                cursor.execute(
                    """SELECT count(*)
                       FROM pg_trigger
                       WHERE NOT tgisinternal AND tgname = ANY(%s)""",
                    (list(_CONTROL_TRIGGERS),),
                )
                trigger = cursor.fetchone()
                trigger_ready = trigger is not None and int(trigger[0]) == len(
                    _CONTROL_TRIGGERS
                )
                return True, trigger_ready


def _verify_session_risk_schema(dsn: str) -> tuple[bool, bool]:
    if psycopg is None:
        raise RuntimeError("PostgreSQL dependency is unavailable")
    with psycopg.connect(dsn, autocommit=False) as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                relation_ready = True
                for relation_name in _SESSION_RISK_RELATIONS:
                    cursor.execute("SELECT to_regclass(%s)", (relation_name,))
                    relation = cursor.fetchone()
                    relation_ready = relation_ready and (
                        relation is not None and relation[0] is not None
                    )
                if not relation_ready:
                    return False, False
                cursor.execute(
                    """SELECT count(*)
                       FROM pg_trigger
                       WHERE NOT tgisinternal AND tgname = ANY(%s)""",
                    (list(_SESSION_RISK_TRIGGERS),),
                )
                trigger = cursor.fetchone()
                trigger_ready = trigger is not None and int(trigger[0]) == len(
                    _SESSION_RISK_TRIGGERS
                )
                return True, trigger_ready


def _verify_recovery_schema(dsn: str) -> tuple[bool, bool]:
    if psycopg is None:
        raise RuntimeError("PostgreSQL dependency is unavailable")
    with psycopg.connect(dsn, autocommit=False) as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT to_regclass(%s)", (_RECOVERY_RELATION,))
                relation = cursor.fetchone()
                relation_ready = relation is not None and relation[0] is not None
                if not relation_ready:
                    return False, False
                cursor.execute(
                    """SELECT count(*)
                       FROM pg_trigger
                       WHERE NOT tgisinternal AND tgname = ANY(%s)""",
                    (list(_RECOVERY_TRIGGERS),),
                )
                trigger = cursor.fetchone()
                trigger_ready = trigger is not None and int(trigger[0]) == len(
                    _RECOVERY_TRIGGERS
                )
                return True, trigger_ready


def _verify_identity_schema(dsn: str) -> tuple[bool, bool, bool]:
    if psycopg is None:
        raise RuntimeError("PostgreSQL dependency is unavailable")
    with psycopg.connect(dsn, autocommit=False) as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT to_regclass(%s)", (_IDENTITY_RELATION,))
                relation = cursor.fetchone()
                relation_ready = relation is not None and relation[0] is not None
                if not relation_ready:
                    return False, False, False
                cursor.execute(
                    """SELECT count(*)
                       FROM pg_trigger
                       WHERE NOT tgisinternal AND tgname = ANY(%s)""",
                    (list(_IDENTITY_TRIGGERS),),
                )
                trigger = cursor.fetchone()
                trigger_ready = trigger is not None and int(trigger[0]) == len(
                    _IDENTITY_TRIGGERS
                )
    if not trigger_ready:
        return True, False, False
    try:
        PostgresBybitDemoOperationalDatabaseIdentityReader(dsn).read_identity()
    except (RuntimeError, ValueError):
        return True, True, False
    return True, True, True


def _migration_fingerprints() -> tuple[BybitDemoPostgresMigrationFingerprint, ...]:
    result: list[BybitDemoPostgresMigrationFingerprint] = []
    for version, path in _MIGRATIONS:
        content = path.read_bytes()
        result.append(
            BybitDemoPostgresMigrationFingerprint(
                version=version,
                path=path.as_posix(),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(result)


__all__ = [
    "BybitDemoPostgresBootstrapResult",
    "BybitDemoPostgresBootstrapStatus",
    "BybitDemoPostgresMigrationFingerprint",
    "apply_bybit_demo_postgres_bootstrap",
    "verify_bybit_demo_postgres_schema",
]
