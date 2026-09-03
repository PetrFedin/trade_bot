from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.execution.bybit_demo_postgres_runtime_role import (
    PostgresBybitDemoRuntimeRolePolicy,
    PostgresBybitDemoRuntimeRolePreflight,
)

try:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    sql = None
    dict_row = None

_SCHEMA = "public"
_MUTATION_FUNCTION = "astra_reject_bybit_demo_audit_mutation_v120"
_APPROVAL_TABLE = "astra_bybit_demo_approved_entry_authorization_v120"
_PROVENANCE_TABLE = "astra_bybit_demo_entry_provenance_v120"
_TERMINAL_TABLE = "astra_bybit_demo_terminal_evidence_v120"
_EXPECTED_TABLE_PRIVILEGES = frozenset({"SELECT", "INSERT"})
_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
_TRUNCATE_TRIGGERS = {
    _APPROVAL_TABLE: "astra_bybit_demo_approval_no_truncate_v120",
    _PROVENANCE_TABLE: "astra_bybit_demo_provenance_no_truncate_v120",
    _TERMINAL_TABLE: "astra_bybit_demo_terminal_no_truncate_v120",
}


@dataclass(frozen=True)
class BybitDemoPostgresAuditRoleEvidence:
    runtime_role: str
    connected_role: str
    bootstrap_role: str
    ready: bool
    reasons: tuple[str, ...]
    base_v119_ready: bool
    base_v119_reasons: tuple[str, ...]
    runtime_owned_tables: tuple[str, ...]
    owner_role_memberships: tuple[str, ...]
    approval_privileges: tuple[str, ...]
    provenance_privileges: tuple[str, ...]
    terminal_privileges: tuple[str, ...]
    truncate_hardened_tables: tuple[str, ...]
    mutation_function_execute: bool
    automatic_role_creation_allowed: bool = False
    runtime_ddl_allowed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "reasons",
            "base_v119_reasons",
            "runtime_owned_tables",
            "owner_role_memberships",
            "approval_privileges",
            "provenance_privileges",
            "terminal_privileges",
            "truncate_hardened_tables",
        ):
            payload[key] = list(payload[key])
        return payload


class PostgresBybitDemoAuditRolePolicy:
    """Bootstrap-only v120 extension of the already-qualified v119 runtime-role policy."""

    automatic_role_creation_allowed = False
    runtime_ddl_allowed = False
    order_writes_supported = False
    live_mainnet_order_routing_allowed = False

    def __init__(self, bootstrap_dsn: str) -> None:
        if not bootstrap_dsn.strip():
            raise ValueError("bootstrap PostgreSQL DSN is required")
        self._bootstrap_dsn = bootstrap_dsn
        self._base_policy = PostgresBybitDemoRuntimeRolePolicy(bootstrap_dsn)

    def _connect(self):
        _require_postgres_dependency()
        return psycopg.connect(self._bootstrap_dsn, row_factory=dict_row, autocommit=False)

    def inspect(self, *, runtime_role: str) -> BybitDemoPostgresAuditRoleEvidence:
        base = self._base_policy.inspect(runtime_role=runtime_role)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                bootstrap_role = _current_user(cursor)
                return _inspect_audit_role(
                    cursor,
                    runtime_role=runtime_role,
                    connected_role=bootstrap_role,
                    bootstrap_role=bootstrap_role,
                    base_ready=base.ready,
                    base_reasons=base.reasons,
                    require_connected_runtime=False,
                )

    def reconcile(self, *, runtime_role: str) -> BybitDemoPostgresAuditRoleEvidence:
        """Reconcile exact v119 plus v120 grants without creating or altering roles."""

        base = self._base_policy.reconcile(runtime_role=runtime_role)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    bootstrap_role = _current_user(cursor)
                    _raise_if_audit_schema_unsafe(cursor, runtime_role=runtime_role)
                    _reconcile_direct_audit_privileges(cursor, runtime_role=runtime_role)
                    evidence = _inspect_audit_role(
                        cursor,
                        runtime_role=runtime_role,
                        connected_role=bootstrap_role,
                        bootstrap_role=bootstrap_role,
                        base_ready=base.ready,
                        base_reasons=base.reasons,
                        require_connected_runtime=False,
                    )
                    if not evidence.ready:
                        joined = ",".join(evidence.reasons)
                        raise RuntimeError(f"v120 runtime audit role policy is not ready:{joined}")
                    return evidence


class PostgresBybitDemoAuditRolePreflight:
    """Read-only preflight for the connected v119+v120 least-privilege runtime credential."""

    automatic_role_creation_allowed = False
    runtime_ddl_allowed = False
    order_writes_supported = False
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        runtime_dsn: str,
        *,
        expected_runtime_role: str,
        expected_bootstrap_role: str,
    ) -> None:
        if not runtime_dsn.strip():
            raise ValueError("runtime PostgreSQL DSN is required")
        self._runtime_dsn = runtime_dsn
        self._expected_runtime_role = expected_runtime_role
        self._expected_bootstrap_role = expected_bootstrap_role
        self._base_preflight = PostgresBybitDemoRuntimeRolePreflight(
            runtime_dsn,
            expected_runtime_role=expected_runtime_role,
            expected_bootstrap_role=expected_bootstrap_role,
        )

    def inspect(self) -> BybitDemoPostgresAuditRoleEvidence:
        base = self._base_preflight.inspect()
        _require_postgres_dependency()
        with psycopg.connect(
            self._runtime_dsn,
            row_factory=dict_row,
            autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                connected_role = _current_user(cursor)
                return _inspect_audit_role(
                    cursor,
                    runtime_role=self._expected_runtime_role,
                    connected_role=connected_role,
                    bootstrap_role=self._expected_bootstrap_role,
                    base_ready=base.ready,
                    base_reasons=base.reasons,
                    require_connected_runtime=True,
                )

    def require_ready(self) -> BybitDemoPostgresAuditRoleEvidence:
        evidence = self.inspect()
        if not evidence.ready:
            joined = ",".join(evidence.reasons)
            raise RuntimeError(f"v120 runtime audit role preflight is not ready:{joined}")
        return evidence


def _require_postgres_dependency() -> None:
    if psycopg is None or sql is None or dict_row is None:
        raise RuntimeError("PostgreSQL dependency is unavailable")


def _current_user(cursor) -> str:
    row = cursor.execute("SELECT current_user AS role_name").fetchone()
    if row is None or not isinstance(row["role_name"], str):
        raise RuntimeError("PostgreSQL current_user is unavailable")
    return row["role_name"]


def _audit_table_owners(cursor) -> dict[str, str]:
    tables = list(_TRUNCATE_TRIGGERS)
    rows = cursor.execute(
        """SELECT c.relname, pg_get_userbyid(c.relowner) AS owner
           FROM pg_class c
           JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname=%s AND c.relname = ANY(%s)""",
        (_SCHEMA, tables),
    ).fetchall()
    owners = {row["relname"]: row["owner"] for row in rows}
    missing = sorted(set(tables) - set(owners))
    if missing:
        raise RuntimeError(f"required v120 PostgreSQL tables are missing:{','.join(missing)}")
    return owners


def _is_member(cursor, *, member: str, target: str) -> bool:
    row = cursor.execute(
        "SELECT pg_has_role(%s, %s, 'MEMBER') AS is_member",
        (member, target),
    ).fetchone()
    return bool(row["is_member"])


def _effective_table_privileges(cursor, *, role: str, table: str) -> frozenset[str]:
    qualified = f"{_SCHEMA}.{table}"
    granted = {
        privilege
        for privilege in _TABLE_PRIVILEGES
        if cursor.execute(
            "SELECT has_table_privilege(%s, %s, %s) AS allowed",
            (role, qualified, privilege),
        ).fetchone()["allowed"]
    }
    return frozenset(granted)


def _truncate_hardening_present(cursor, *, table: str, trigger_name: str) -> bool:
    row = cursor.execute(
        """SELECT count(*) AS trigger_count
           FROM pg_trigger t
           JOIN pg_class c ON c.oid=t.tgrelid
           JOIN pg_namespace n ON n.oid=c.relnamespace
           JOIN pg_proc p ON p.oid=t.tgfoid
           JOIN pg_namespace pn ON pn.oid=p.pronamespace
           WHERE n.nspname=%s
             AND c.relname=%s
             AND t.tgname=%s
             AND NOT t.tgisinternal
             AND t.tgenabled IN ('O', 'A')
             AND (t.tgtype & 32) = 32
             AND (t.tgtype & 2) = 2
             AND (t.tgtype & 1) = 0
             AND pn.nspname=%s
             AND p.proname=%s""",
        (_SCHEMA, table, trigger_name, _SCHEMA, _MUTATION_FUNCTION),
    ).fetchone()
    return row is not None and row["trigger_count"] == 1


def _mutation_function_execute(cursor, *, role: str) -> bool:
    row = cursor.execute(
        "SELECT has_function_privilege(%s, %s, 'EXECUTE') AS allowed",
        (role, f"{_SCHEMA}.{_MUTATION_FUNCTION}()"),
    ).fetchone()
    return bool(row["allowed"])


def _raise_if_audit_schema_unsafe(cursor, *, runtime_role: str) -> None:
    owners = _audit_table_owners(cursor)
    reasons: list[str] = []
    for table, owner in sorted(owners.items()):
        if owner == runtime_role:
            reasons.append(f"RUNTIME_ROLE_OWNS_V120_TABLE:{table}")
        elif _is_member(cursor, member=runtime_role, target=owner):
            reasons.append(f"RUNTIME_ROLE_MEMBER_OF_V120_OWNER:{owner}")

    for table, trigger_name in sorted(_TRUNCATE_TRIGGERS.items()):
        if not _truncate_hardening_present(cursor, table=table, trigger_name=trigger_name):
            reasons.append(f"V120_TRUNCATE_HARDENING_MISSING:{table}")

    if reasons:
        raise ValueError("unsafe v120 PostgreSQL audit boundary:" + ",".join(reasons))


def _reconcile_direct_audit_privileges(cursor, *, runtime_role: str) -> None:
    for table in _TRUNCATE_TRIGGERS:
        cursor.execute(
            sql.SQL("REVOKE ALL PRIVILEGES ON TABLE {}.{} FROM {}").format(
                sql.Identifier(_SCHEMA),
                sql.Identifier(table),
                sql.Identifier(runtime_role),
            )
        )
        cursor.execute(
            sql.SQL("GRANT SELECT, INSERT ON TABLE {}.{} TO {}").format(
                sql.Identifier(_SCHEMA),
                sql.Identifier(table),
                sql.Identifier(runtime_role),
            )
        )
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON FUNCTION {}.{}() FROM {}").format(
            sql.Identifier(_SCHEMA),
            sql.Identifier(_MUTATION_FUNCTION),
            sql.Identifier(runtime_role),
        )
    )


def _inspect_audit_role(
    cursor,
    *,
    runtime_role: str,
    connected_role: str,
    bootstrap_role: str,
    base_ready: bool,
    base_reasons: tuple[str, ...],
    require_connected_runtime: bool,
) -> BybitDemoPostgresAuditRoleEvidence:
    reasons = [f"BASE_V119:{reason}" for reason in base_reasons]
    if require_connected_runtime and connected_role != runtime_role:
        reason = "CONNECTED_ROLE_MISMATCH"
        if reason not in reasons:
            reasons.append(reason)

    owners = _audit_table_owners(cursor)
    runtime_owned_tables = tuple(
        sorted(table for table, owner in owners.items() if owner == runtime_role)
    )
    for table in runtime_owned_tables:
        reasons.append(f"RUNTIME_ROLE_OWNS_V120_TABLE:{table}")

    owner_role_memberships = tuple(
        sorted(
            owner
            for owner in set(owners.values())
            if owner != runtime_role and _is_member(cursor, member=runtime_role, target=owner)
        )
    )
    for owner in owner_role_memberships:
        reasons.append(f"RUNTIME_ROLE_MEMBER_OF_V120_OWNER:{owner}")

    actual_privileges = {
        table: _effective_table_privileges(cursor, role=runtime_role, table=table)
        for table in _TRUNCATE_TRIGGERS
    }
    for table, actual in sorted(actual_privileges.items()):
        if actual != _EXPECTED_TABLE_PRIVILEGES:
            reasons.append(
                f"V120_TABLE_PRIVILEGE_MISMATCH:{table}:"
                + ",".join(sorted(actual))
            )

    truncate_hardened_tables = tuple(
        sorted(
            table
            for table, trigger_name in _TRUNCATE_TRIGGERS.items()
            if _truncate_hardening_present(cursor, table=table, trigger_name=trigger_name)
        )
    )
    missing_hardening = sorted(set(_TRUNCATE_TRIGGERS) - set(truncate_hardened_tables))
    for table in missing_hardening:
        reasons.append(f"V120_TRUNCATE_HARDENING_MISSING:{table}")

    mutation_function_execute = _mutation_function_execute(cursor, role=runtime_role)
    if mutation_function_execute:
        reasons.append("RUNTIME_ROLE_UNEXPECTED_V120_MUTATION_FUNCTION_EXECUTE")

    return BybitDemoPostgresAuditRoleEvidence(
        runtime_role=runtime_role,
        connected_role=connected_role,
        bootstrap_role=bootstrap_role,
        ready=base_ready and not reasons,
        reasons=tuple(reasons),
        base_v119_ready=base_ready,
        base_v119_reasons=tuple(base_reasons),
        runtime_owned_tables=runtime_owned_tables,
        owner_role_memberships=owner_role_memberships,
        approval_privileges=tuple(sorted(actual_privileges[_APPROVAL_TABLE])),
        provenance_privileges=tuple(sorted(actual_privileges[_PROVENANCE_TABLE])),
        terminal_privileges=tuple(sorted(actual_privileges[_TERMINAL_TABLE])),
        truncate_hardened_tables=truncate_hardened_tables,
        mutation_function_execute=mutation_function_execute,
    )


__all__ = [
    "BybitDemoPostgresAuditRoleEvidence",
    "PostgresBybitDemoAuditRolePolicy",
    "PostgresBybitDemoAuditRolePreflight",
]
