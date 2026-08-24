from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_connected_preflight import (
    PostgresBybitDemoOperationalStateReader,
)

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None

_CONFIRMATION_PHRASE = "APPLY_BYBIT_DEMO_V119_V120"
_ADVISORY_LOCK_KEY = 119120
_MIGRATIONS = (
    ("v119", Path("migrations/v119/001_bybit_demo_durable_runtime.sql")),
    ("v120", Path("migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql")),
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
            "schema": "BYBIT_DEMO_POSTGRES_BOOTSTRAP_V1",
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
            "database_identity_exposed": self.database_identity_exposed,
            "bybit_credentials_required": self.bybit_credentials_required,
            "bybit_order_writes_supported": self.bybit_order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


def verify_bybit_demo_postgres_schema(dsn: str) -> BybitDemoPostgresBootstrapResult:
    """Verify the durable Demo runtime/audit schema without modifying PostgreSQL."""

    fingerprints = _migration_fingerprints()
    state = PostgresBybitDemoOperationalStateReader(dsn).read_state()
    ready = state.required_relations_present and state.append_only_triggers_present
    return BybitDemoPostgresBootstrapResult(
        status=(
            BybitDemoPostgresBootstrapStatus.VERIFIED_READY
            if ready
            else BybitDemoPostgresBootstrapStatus.SCHEMA_NOT_READY
        ),
        schema_mutation_performed=False,
        required_relations_present=state.required_relations_present,
        append_only_triggers_present=state.append_only_triggers_present,
        migration_fingerprints=fingerprints,
    )


def apply_bybit_demo_postgres_bootstrap(
    dsn: str,
    *,
    confirmation_phrase: str,
) -> BybitDemoPostgresBootstrapResult:
    """Apply exactly v119 then v120 under a session advisory lock and verify afterward."""

    if confirmation_phrase != _CONFIRMATION_PHRASE:
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
            for _version, path in _MIGRATIONS:
                connection.execute(path.read_text(encoding="utf-8"))
        finally:
            connection.execute(
                "SELECT pg_advisory_unlock(%s)",
                (_ADVISORY_LOCK_KEY,),
            )

    state = PostgresBybitDemoOperationalStateReader(dsn).read_state()
    if not state.required_relations_present:
        raise RuntimeError("Bybit Demo PostgreSQL bootstrap relations verification failed")
    if not state.append_only_triggers_present:
        raise RuntimeError("Bybit Demo PostgreSQL bootstrap append-only verification failed")
    return BybitDemoPostgresBootstrapResult(
        status=BybitDemoPostgresBootstrapStatus.APPLIED_AND_VERIFIED,
        schema_mutation_performed=True,
        required_relations_present=True,
        append_only_triggers_present=True,
        migration_fingerprints=fingerprints,
    )


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
