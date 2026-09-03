from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

try:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    sql = None
    dict_row = None

_SCHEMA = "public"
_LEASE_TABLE = "astra_bybit_demo_runtime_lease_v119"
_EXCURSION_TABLE = "astra_bybit_demo_active_excursion_v119"
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
_EXPECTED_TABLE_PRIVILEGES = {
    _LEASE_TABLE: frozenset({"SELECT", "INSERT", "DELETE"}),
    _EXCURSION_TABLE: frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
}


@dataclass(frozen=True)
class BybitDemoPostgresRuntimeRoleEvidence:
    runtime_role: str
    connected_role: str
    bootstrap_role: str
    ready: bool
    reasons: tuple[str, ...]
    database_create: bool
    schema_usage: bool
    schema_create: bool
    runtime_owns_schema: bool
    runtime_owned_tables: tuple[str, ...]
    role_memberships: tuple[str, ...]
    owner_role_memberships: tuple[str, ...]
    lease_privileges: tuple[str, ...]
    excursion_privileges: tuple[str, ...]
    runtime_superuser: bool
    runtime_createdb: bool
    runtime_createrole: bool
    runtime_replication: bool
    runtime_bypassrls: bool
    runtime_can_login: bool
    automatic_role_creation_allowed: bool = False
    runtime_ddl_allowed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "reasons",
            "runtime_owned_tables",
            "role_memberships",
            "owner_role_memberships",
            "lease_privileges",
            "excursion_privileges",
        ):
            payload[key] = list(payload[key])
        return payload


class PostgresBybitDemoRuntimeRolePolicy:
    """Bootstrap-only reconciler for the exact v119 runtime-role privilege boundary."""

    automatic_role_creation_allowed = False
    runtime_ddl_allowed = False
    order_writes_supported = False
    live_mainnet_order_routing_allowed = False

    def __init__(self, bootstrap_dsn: str) -> None:
        if not bootstrap_dsn.strip():
            raise ValueError("bootstrap PostgreSQL DSN is required")
        self._bootstrap_dsn = bootstrap_dsn

    def _connect(self):
        _require_postgres_dependency()
        return psycopg.connect(self._bootstrap_dsn, row_factory=dict_row, autocommit=False)

    def inspect(self, *, runtime_role: str) -> BybitDemoPostgresRuntimeRoleEvidence:
        _validate_role_name(runtime_role)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                bootstrap_role = _current_user(cursor)
                return _inspect_role(
                    cursor,
                    runtime_role=runtime_role,
                    connected_role=bootstrap_role,
                    bootstrap_role=bootstrap_role,
                    require_connected_runtime=False,
                )

    def reconcile(self, *, runtime_role: str) -> BybitDemoPostgresRuntimeRoleEvidence:
        """Apply only object-level v119 grants; never create/alter a PostgreSQL role."""

        _validate_role_name(runtime_role)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    bootstrap_role = _current_user(cursor)
                    if runtime_role == bootstrap_role:
                        raise ValueError("runtime role must differ from bootstrap role")
                    _raise_if_structurally_unsafe(
                        cursor,
                        runtime_role=runtime_role,
                        bootstrap_role=bootstrap_role,
                    )
                    _reconcile_direct_privileges(cursor, runtime_role=runtime_role)
                    evidence = _inspect_role(
                        cursor,
                        runtime_role=runtime_role,
                        connected_role=bootstrap_role,
                        bootstrap_role=bootstrap_role,
                        require_connected_runtime=False,
                    )
                    if not evidence.ready:
                        joined = ",".join(evidence.reasons)
                        raise RuntimeError(f"runtime role privilege policy is not ready:{joined}")
                    return evidence


class PostgresBybitDemoRuntimeRolePreflight:
    """Read-only preflight proving the actual runtime credential has the approved v119 role."""

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
        _validate_role_name(expected_runtime_role)
        _validate_role_name(expected_bootstrap_role)
        if expected_runtime_role == expected_bootstrap_role:
            raise ValueError("runtime role must differ from bootstrap role")
        self._runtime_dsn = runtime_dsn
        self._expected_runtime_role = expected_runtime_role
        self._expected_bootstrap_role = expected_bootstrap_role

    def inspect(self) -> BybitDemoPostgresRuntimeRoleEvidence:
        _require_postgres_dependency()
        with psycopg.connect(
            self._runtime_dsn,
            row_factory=dict_row,
            autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                connected_role = _current_user(cursor)
                return _inspect_role(
                    cursor,
                    runtime_role=self._expected_runtime_role,
                    connected_role=connected_role,
                    bootstrap_role=self._expected_bootstrap_role,
                    require_connected_runtime=True,
                )

    def require_ready(self) -> BybitDemoPostgresRuntimeRoleEvidence:
        evidence = self.inspect()
        if not evidence.ready:
            joined = ",".join(evidence.reasons)
            raise RuntimeError(f"runtime PostgreSQL role preflight is not ready:{joined}")
        return evidence


def _require_postgres_dependency() -> None:
    if psycopg is None or sql is None or dict_row is None:
        raise RuntimeError("PostgreSQL dependency is unavailable")


def _validate_role_name(role: str) -> None:
    if not isinstance(role, str) or _ROLE_NAME.fullmatch(role) is None:
        raise ValueError("runtime PostgreSQL role must be a lowercase safe identifier")


def _current_user(cursor) -> str:
    row = cursor.execute("SELECT current_user AS role_name").fetchone()
    if row is None or not isinstance(row["role_name"], str):
        raise RuntimeError("PostgreSQL current_user is unavailable")
    return row["role_name"]


def _current_database(cursor) -> str:
    row = cursor.execute("SELECT current_database() AS database_name").fetchone()
    if row is None or not isinstance(row["database_name"], str):
        raise RuntimeError("PostgreSQL current_database is unavailable")
    return row["database_name"]


def _role_attributes(cursor, role: str) -> dict[str, Any]:
    row = cursor.execute(
        """SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin,
                  rolreplication, rolbypassrls
           FROM pg_roles
           WHERE rolname=%s""",
        (role,),
    ).fetchone()
    if row is None:
        raise ValueError("runtime PostgreSQL role does not exist")
    return dict(row)


def _explicit_role_memberships(cursor, *, role: str) -> tuple[str, ...]:
    rows = cursor.execute(
        """SELECT parent.rolname
           FROM pg_auth_members membership
           JOIN pg_roles child ON child.oid=membership.member
           JOIN pg_roles parent ON parent.oid=membership.roleid
           WHERE child.rolname=%s
           ORDER BY parent.rolname""",
        (role,),
    ).fetchall()
    return tuple(row["rolname"] for row in rows)


def _object_owners(cursor) -> tuple[str, dict[str, str]]:
    schema_row = cursor.execute(
        """SELECT pg_get_userbyid(nspowner) AS owner
           FROM pg_namespace
           WHERE nspname=%s""",
        (_SCHEMA,),
    ).fetchone()
    if schema_row is None:
        raise RuntimeError("public PostgreSQL schema is unavailable")
    schema_owner = schema_row["owner"]

    rows = cursor.execute(
        """SELECT c.relname, pg_get_userbyid(c.relowner) AS owner
           FROM pg_class c
           JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname=%s AND c.relname = ANY(%s)""",
        (_SCHEMA, [_LEASE_TABLE, _EXCURSION_TABLE]),
    ).fetchall()
    owners = {row["relname"]: row["owner"] for row in rows}
    missing = sorted(set(_EXPECTED_TABLE_PRIVILEGES) - set(owners))
    if missing:
        raise RuntimeError(f"required v119 PostgreSQL tables are missing:{','.join(missing)}")
    return schema_owner, owners


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


def _schema_privilege(cursor, *, role: str, privilege: str) -> bool:
    row = cursor.execute(
        "SELECT has_schema_privilege(%s, %s, %s) AS allowed",
        (role, _SCHEMA, privilege),
    ).fetchone()
    return bool(row["allowed"])


def _database_privilege(cursor, *, role: str, privilege: str) -> bool:
    row = cursor.execute(
        "SELECT has_database_privilege(%s, current_database(), %s) AS allowed",
        (role, privilege),
    ).fetchone()
    return bool(row["allowed"])


def _raise_if_structurally_unsafe(cursor, *, runtime_role: str, bootstrap_role: str) -> None:
    attrs = _role_attributes(cursor, runtime_role)
    reasons: list[str] = []
    if attrs["rolsuper"]:
        reasons.append("RUNTIME_ROLE_SUPERUSER")
    if attrs["rolcreatedb"]:
        reasons.append("RUNTIME_ROLE_CREATEDB")
    if attrs["rolcreaterole"]:
        reasons.append("RUNTIME_ROLE_CREATEROLE")
    if attrs["rolreplication"]:
        reasons.append("RUNTIME_ROLE_REPLICATION")
    if attrs["rolbypassrls"]:
        reasons.append("RUNTIME_ROLE_BYPASSRLS")
    if not attrs["rolcanlogin"]:
        reasons.append("RUNTIME_ROLE_NOT_LOGIN")

    memberships = _explicit_role_memberships(cursor, role=runtime_role)
    for membership in memberships:
        reasons.append(f"RUNTIME_ROLE_HAS_MEMBERSHIP:{membership}")

    schema_owner, table_owners = _object_owners(cursor)
    if schema_owner == runtime_role:
        reasons.append("RUNTIME_ROLE_OWNS_SCHEMA")
    for table, owner in sorted(table_owners.items()):
        if owner == runtime_role:
            reasons.append(f"RUNTIME_ROLE_OWNS_TABLE:{table}")

    owner_roles = {schema_owner, *table_owners.values(), bootstrap_role}
    for owner in sorted(owner for owner in owner_roles if owner != runtime_role):
        if _is_member(cursor, member=runtime_role, target=owner):
            reason = f"RUNTIME_ROLE_MEMBER_OF_OWNER:{owner}"
            if reason not in reasons:
                reasons.append(reason)

    if reasons:
        raise ValueError("unsafe runtime PostgreSQL role:" + ",".join(reasons))


def _reconcile_direct_privileges(cursor, *, runtime_role: str) -> None:
    database_name = _current_database(cursor)
    cursor.execute(
        sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
            sql.Identifier(database_name),
            sql.Identifier(runtime_role),
        )
    )
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(
            sql.Identifier(_SCHEMA),
            sql.Identifier(runtime_role),
        )
    )
    cursor.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(_SCHEMA),
            sql.Identifier(runtime_role),
        )
    )

    for table, privileges in _EXPECTED_TABLE_PRIVILEGES.items():
        cursor.execute(
            sql.SQL("REVOKE ALL PRIVILEGES ON TABLE {}.{} FROM {}").format(
                sql.Identifier(_SCHEMA),
                sql.Identifier(table),
                sql.Identifier(runtime_role),
            )
        )
        privilege_sql = sql.SQL(", ").join(sql.SQL(privilege) for privilege in sorted(privileges))
        cursor.execute(
            sql.SQL("GRANT {} ON TABLE {}.{} TO {}").format(
                privilege_sql,
                sql.Identifier(_SCHEMA),
                sql.Identifier(table),
                sql.Identifier(runtime_role),
            )
        )


def _inspect_role(
    cursor,
    *,
    runtime_role: str,
    connected_role: str,
    bootstrap_role: str,
    require_connected_runtime: bool,
) -> BybitDemoPostgresRuntimeRoleEvidence:
    attrs = _role_attributes(cursor, runtime_role)
    schema_owner, table_owners = _object_owners(cursor)
    memberships = _explicit_role_memberships(cursor, role=runtime_role)

    reasons: list[str] = []
    if require_connected_runtime and connected_role != runtime_role:
        reasons.append("CONNECTED_ROLE_MISMATCH")
    if runtime_role == bootstrap_role:
        reasons.append("BOOTSTRAP_RUNTIME_ROLE_NOT_SEPARATED")
    if attrs["rolsuper"]:
        reasons.append("RUNTIME_ROLE_SUPERUSER")
    if attrs["rolcreatedb"]:
        reasons.append("RUNTIME_ROLE_CREATEDB")
    if attrs["rolcreaterole"]:
        reasons.append("RUNTIME_ROLE_CREATEROLE")
    if attrs["rolreplication"]:
        reasons.append("RUNTIME_ROLE_REPLICATION")
    if attrs["rolbypassrls"]:
        reasons.append("RUNTIME_ROLE_BYPASSRLS")
    if not attrs["rolcanlogin"]:
        reasons.append("RUNTIME_ROLE_NOT_LOGIN")
    for membership in memberships:
        reasons.append(f"RUNTIME_ROLE_HAS_MEMBERSHIP:{membership}")

    database_create = _database_privilege(cursor, role=runtime_role, privilege="CREATE")
    schema_usage = _schema_privilege(cursor, role=runtime_role, privilege="USAGE")
    schema_create = _schema_privilege(cursor, role=runtime_role, privilege="CREATE")
    if database_create:
        reasons.append("RUNTIME_ROLE_UNEXPECTED_DATABASE_CREATE")
    if not schema_usage:
        reasons.append("RUNTIME_ROLE_MISSING_SCHEMA_USAGE")
    if schema_create:
        reasons.append("RUNTIME_ROLE_UNEXPECTED_SCHEMA_CREATE")

    runtime_owns_schema = schema_owner == runtime_role
    if runtime_owns_schema:
        reasons.append("RUNTIME_ROLE_OWNS_SCHEMA")

    runtime_owned_tables = tuple(
        sorted(table for table, owner in table_owners.items() if owner == runtime_role)
    )
    for table in runtime_owned_tables:
        reasons.append(f"RUNTIME_ROLE_OWNS_TABLE:{table}")

    owner_roles = {schema_owner, *table_owners.values(), bootstrap_role}
    owner_role_memberships = tuple(
        sorted(owner for owner in owner_roles if owner != runtime_role and owner in memberships)
    )

    actual_privileges: dict[str, frozenset[str]] = {}
    for table, expected in _EXPECTED_TABLE_PRIVILEGES.items():
        actual = _effective_table_privileges(cursor, role=runtime_role, table=table)
        actual_privileges[table] = actual
        for privilege in sorted(expected - actual):
            reasons.append(f"{table}:MISSING_PRIVILEGE:{privilege}")
        for privilege in sorted(actual - expected):
            reasons.append(f"{table}:UNEXPECTED_PRIVILEGE:{privilege}")

    return BybitDemoPostgresRuntimeRoleEvidence(
        runtime_role=runtime_role,
        connected_role=connected_role,
        bootstrap_role=bootstrap_role,
        ready=not reasons,
        reasons=tuple(reasons),
        database_create=database_create,
        schema_usage=schema_usage,
        schema_create=schema_create,
        runtime_owns_schema=runtime_owns_schema,
        runtime_owned_tables=runtime_owned_tables,
        role_memberships=memberships,
        owner_role_memberships=owner_role_memberships,
        lease_privileges=tuple(sorted(actual_privileges[_LEASE_TABLE])),
        excursion_privileges=tuple(sorted(actual_privileges[_EXCURSION_TABLE])),
        runtime_superuser=bool(attrs["rolsuper"]),
        runtime_createdb=bool(attrs["rolcreatedb"]),
        runtime_createrole=bool(attrs["rolcreaterole"]),
        runtime_replication=bool(attrs["rolreplication"]),
        runtime_bypassrls=bool(attrs["rolbypassrls"]),
        runtime_can_login=bool(attrs["rolcanlogin"]),
    )


__all__ = [
    "BybitDemoPostgresRuntimeRoleEvidence",
    "PostgresBybitDemoRuntimeRolePolicy",
    "PostgresBybitDemoRuntimeRolePreflight",
]
