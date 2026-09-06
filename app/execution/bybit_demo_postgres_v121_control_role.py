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
_TABLE = "astra_bybit_demo_control_event_v121"
_SEQUENCE = "astra_bybit_demo_control_event_v121_event_seq_seq"
_MUTATION_FUNCTION = "astra_reject_bybit_demo_control_mutation_v121"
_APPEND_TRIGGER = "astra_bybit_demo_control_append_only_v121"
_TRUNCATE_TRIGGER = "astra_bybit_demo_control_no_truncate_v121"
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
_SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")
_READER_TABLE = frozenset({"SELECT"})
_WRITER_TABLE = frozenset({"SELECT", "INSERT"})
_READER_SEQUENCE = frozenset()
_WRITER_SEQUENCE = frozenset({"USAGE"})


@dataclass(frozen=True)
class BybitDemoPostgresControlRoleStateV121:
    role: str
    ready: bool
    reasons: tuple[str, ...]
    table_privileges: tuple[str, ...]
    sequence_privileges: tuple[str, ...]
    database_create: bool
    schema_usage: bool
    schema_create: bool
    owns_schema: bool
    owns_table: bool
    owns_sequence: bool
    role_memberships: tuple[str, ...]
    owner_role_memberships: tuple[str, ...]
    mutation_function_execute: bool
    superuser: bool
    createdb: bool
    createrole: bool
    replication: bool
    bypassrls: bool
    can_login: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "reasons",
            "table_privileges",
            "sequence_privileges",
            "role_memberships",
            "owner_role_memberships",
        ):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class BybitDemoPostgresControlRoleEvidenceV121:
    reader: BybitDemoPostgresControlRoleStateV121
    writer: BybitDemoPostgresControlRoleStateV121
    bootstrap_role: str
    append_trigger_ready: bool
    truncate_trigger_ready: bool
    ready: bool
    automatic_role_creation_allowed: bool = False
    runtime_ddl_allowed: bool = False
    order_writes_supported: bool = False
    broker_network_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reader": self.reader.to_dict(),
            "writer": self.writer.to_dict(),
            "bootstrap_role": self.bootstrap_role,
            "append_trigger_ready": self.append_trigger_ready,
            "truncate_trigger_ready": self.truncate_trigger_ready,
            "ready": self.ready,
            "automatic_role_creation_allowed": self.automatic_role_creation_allowed,
            "runtime_ddl_allowed": self.runtime_ddl_allowed,
            "order_writes_supported": self.order_writes_supported,
            "broker_network_supported": self.broker_network_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


class PostgresBybitDemoControlJournalRolePolicyV121:
    """Bootstrap-only exact privilege reconciler for v121 reader/writer credentials."""

    automatic_role_creation_allowed = False
    runtime_ddl_allowed = False
    order_writes_supported = False
    broker_network_supported = False
    live_mainnet_order_routing_allowed = False

    def __init__(self, bootstrap_dsn: str) -> None:
        if not bootstrap_dsn.strip():
            raise ValueError("bootstrap PostgreSQL DSN is required")
        self._bootstrap_dsn = bootstrap_dsn

    def inspect(
        self,
        *,
        reader_role: str,
        writer_role: str,
    ) -> BybitDemoPostgresControlRoleEvidenceV121:
        _validate_role_pair(reader_role, writer_role)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                return _inspect_pair(
                    cursor,
                    reader_role=reader_role,
                    writer_role=writer_role,
                )

    def reconcile(
        self,
        *,
        reader_role: str,
        writer_role: str,
    ) -> BybitDemoPostgresControlRoleEvidenceV121:
        _validate_role_pair(reader_role, writer_role)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    bootstrap_role = _current_user(cursor)
                    if bootstrap_role in {reader_role, writer_role}:
                        raise ValueError("v121 control roles must differ from bootstrap role")
                    _require_schema_and_hardening(cursor)
                    for role in (reader_role, writer_role):
                        _raise_if_structurally_unsafe(
                            cursor,
                            role=role,
                            bootstrap_role=bootstrap_role,
                        )
                    _reconcile_role(
                        cursor,
                        role=reader_role,
                        table_privileges=_READER_TABLE,
                        sequence_privileges=_READER_SEQUENCE,
                    )
                    _reconcile_role(
                        cursor,
                        role=writer_role,
                        table_privileges=_WRITER_TABLE,
                        sequence_privileges=_WRITER_SEQUENCE,
                    )
                    evidence = _inspect_pair(
                        cursor,
                        reader_role=reader_role,
                        writer_role=writer_role,
                    )
                    if not evidence.ready:
                        reasons = (*evidence.reader.reasons, *evidence.writer.reasons)
                        raise RuntimeError(
                            "v121 control role policy is not ready:" + ",".join(reasons)
                        )
                    return evidence

    def _connect(self):
        _require_postgres_dependency()
        return psycopg.connect(self._bootstrap_dsn, row_factory=dict_row, autocommit=False)


def _validate_role_pair(reader_role: str, writer_role: str) -> None:
    for role in (reader_role, writer_role):
        if not isinstance(role, str) or _ROLE_NAME.fullmatch(role) is None:
            raise ValueError("v121 control PostgreSQL role must be a lowercase safe identifier")
    if reader_role == writer_role:
        raise ValueError("v121 control reader and writer roles must be distinct")


def _require_postgres_dependency() -> None:
    if psycopg is None or sql is None or dict_row is None:
        raise RuntimeError("PostgreSQL dependency is unavailable")


def _current_user(cursor) -> str:
    row = cursor.execute("SELECT current_user AS role_name").fetchone()
    if row is None or not isinstance(row["role_name"], str):
        raise RuntimeError("PostgreSQL current_user is unavailable")
    return row["role_name"]


def _role_attributes(cursor, role: str) -> dict[str, Any]:
    row = cursor.execute(
        """SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin,
                  rolreplication, rolbypassrls
           FROM pg_roles WHERE rolname=%s""",
        (role,),
    ).fetchone()
    if row is None:
        raise ValueError(f"v121 control PostgreSQL role does not exist:{role}")
    return dict(row)


def _memberships(cursor, role: str) -> tuple[str, ...]:
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


def _owners(cursor) -> tuple[str, str, str]:
    schema = cursor.execute(
        "SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname=%s",
        (_SCHEMA,),
    ).fetchone()
    table = cursor.execute(
        """SELECT pg_get_userbyid(c.relowner) AS owner
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname=%s AND c.relname=%s""",
        (_SCHEMA, _TABLE),
    ).fetchone()
    sequence = cursor.execute(
        """SELECT pg_get_userbyid(c.relowner) AS owner
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname=%s AND c.relname=%s AND c.relkind='S'""",
        (_SCHEMA, _SEQUENCE),
    ).fetchone()
    if schema is None or table is None or sequence is None:
        raise RuntimeError("required v121 PostgreSQL control journal objects are missing")
    return schema["owner"], table["owner"], sequence["owner"]


def _is_member(cursor, *, member: str, target: str) -> bool:
    return bool(
        cursor.execute(
            "SELECT pg_has_role(%s, %s, 'MEMBER') AS allowed",
            (member, target),
        ).fetchone()["allowed"]
    )


def _table_privileges(cursor, role: str) -> frozenset[str]:
    qualified = f"{_SCHEMA}.{_TABLE}"
    return frozenset(
        privilege
        for privilege in _TABLE_PRIVILEGES
        if cursor.execute(
            "SELECT has_table_privilege(%s, %s, %s) AS allowed",
            (role, qualified, privilege),
        ).fetchone()["allowed"]
    )


def _sequence_privileges(cursor, role: str) -> frozenset[str]:
    qualified = f"{_SCHEMA}.{_SEQUENCE}"
    return frozenset(
        privilege
        for privilege in _SEQUENCE_PRIVILEGES
        if cursor.execute(
            "SELECT has_sequence_privilege(%s, %s, %s) AS allowed",
            (role, qualified, privilege),
        ).fetchone()["allowed"]
    )


def _schema_privilege(cursor, role: str, privilege: str) -> bool:
    return bool(
        cursor.execute(
            "SELECT has_schema_privilege(%s, %s, %s) AS allowed",
            (role, _SCHEMA, privilege),
        ).fetchone()["allowed"]
    )


def _database_create(cursor, role: str) -> bool:
    return bool(
        cursor.execute(
            "SELECT has_database_privilege(%s, current_database(), 'CREATE') AS allowed",
            (role,),
        ).fetchone()["allowed"]
    )


def _function_execute(cursor, role: str) -> bool:
    return bool(
        cursor.execute(
            "SELECT has_function_privilege(%s, %s, 'EXECUTE') AS allowed",
            (role, f"{_SCHEMA}.{_MUTATION_FUNCTION}()"),
        ).fetchone()["allowed"]
    )


def _trigger_ready(cursor, *, name: str, truncate: bool) -> bool:
    row = cursor.execute(
        """SELECT count(*) AS trigger_count
           FROM pg_trigger t
           JOIN pg_class c ON c.oid=t.tgrelid
           JOIN pg_namespace n ON n.oid=c.relnamespace
           JOIN pg_proc p ON p.oid=t.tgfoid
           JOIN pg_namespace pn ON pn.oid=p.pronamespace
           WHERE n.nspname=%s AND c.relname=%s AND t.tgname=%s
             AND NOT t.tgisinternal AND t.tgenabled IN ('O','A')
             AND pn.nspname=%s
             AND p.proname=%s
             AND (t.tgtype & 2)=2
             AND ((%s AND (t.tgtype & 32)=32 AND (t.tgtype & 1)=0)
                  OR (NOT %s AND (t.tgtype & 8)=8 AND (t.tgtype & 16)=16
                      AND (t.tgtype & 1)=1))""",
        (_SCHEMA, _TABLE, name, _SCHEMA, _MUTATION_FUNCTION, truncate, truncate),
    ).fetchone()
    return row is not None and row["trigger_count"] == 1


def _require_schema_and_hardening(cursor) -> None:
    if not _trigger_ready(cursor, name=_APPEND_TRIGGER, truncate=False):
        raise ValueError("v121 control append-only UPDATE/DELETE trigger is not ready")
    if not _trigger_ready(cursor, name=_TRUNCATE_TRIGGER, truncate=True):
        raise ValueError("v121 control TRUNCATE hardening trigger is not ready")


def _raise_if_structurally_unsafe(cursor, *, role: str, bootstrap_role: str) -> None:
    attrs = _role_attributes(cursor, role)
    reasons: list[str] = []
    if attrs["rolsuper"]:
        reasons.append("SUPERUSER")
    if attrs["rolcreatedb"]:
        reasons.append("CREATEDB")
    if attrs["rolcreaterole"]:
        reasons.append("CREATEROLE")
    if attrs["rolreplication"]:
        reasons.append("REPLICATION")
    if attrs["rolbypassrls"]:
        reasons.append("BYPASSRLS")
    if not attrs["rolcanlogin"]:
        reasons.append("NOT_LOGIN")
    memberships = _memberships(cursor, role)
    for membership in memberships:
        reasons.append(f"ROLE_MEMBERSHIP:{membership}")
    schema_owner, table_owner, sequence_owner = _owners(cursor)
    for label, owner in (
        ("SCHEMA", schema_owner),
        ("TABLE", table_owner),
        ("SEQUENCE", sequence_owner),
    ):
        if owner == role:
            reasons.append(f"OWNS_{label}")
    for owner in {schema_owner, table_owner, sequence_owner, bootstrap_role}:
        if owner != role and _is_member(cursor, member=role, target=owner):
            reasons.append(f"MEMBER_OF_OWNER:{owner}")
    if reasons:
        raise ValueError(f"unsafe v121 control PostgreSQL role {role}:" + ",".join(reasons))


def _reconcile_role(
    cursor,
    *,
    role: str,
    table_privileges: frozenset[str],
    sequence_privileges: frozenset[str],
) -> None:
    database_name = cursor.execute("SELECT current_database() AS name").fetchone()["name"]
    cursor.execute(
        sql.SQL("REVOKE CREATE ON DATABASE {} FROM {}").format(
            sql.Identifier(database_name),
            sql.Identifier(role),
        )
    )
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(
            sql.Identifier(_SCHEMA),
            sql.Identifier(role),
        )
    )
    cursor.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(_SCHEMA),
            sql.Identifier(role),
        )
    )
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON TABLE {}.{} FROM {}").format(
            sql.Identifier(_SCHEMA),
            sql.Identifier(_TABLE),
            sql.Identifier(role),
        )
    )
    cursor.execute(
        sql.SQL("GRANT {} ON TABLE {}.{} TO {}").format(
            sql.SQL(", ").join(sql.SQL(value) for value in sorted(table_privileges)),
            sql.Identifier(_SCHEMA),
            sql.Identifier(_TABLE),
            sql.Identifier(role),
        )
    )
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON SEQUENCE {}.{} FROM {}").format(
            sql.Identifier(_SCHEMA),
            sql.Identifier(_SEQUENCE),
            sql.Identifier(role),
        )
    )
    if sequence_privileges:
        cursor.execute(
            sql.SQL("GRANT {} ON SEQUENCE {}.{} TO {}").format(
                sql.SQL(", ").join(
                    sql.SQL(value) for value in sorted(sequence_privileges)
                ),
                sql.Identifier(_SCHEMA),
                sql.Identifier(_SEQUENCE),
                sql.Identifier(role),
            )
        )
    cursor.execute(
        sql.SQL("REVOKE ALL PRIVILEGES ON FUNCTION {}.{}() FROM {}").format(
            sql.Identifier(_SCHEMA),
            sql.Identifier(_MUTATION_FUNCTION),
            sql.Identifier(role),
        )
    )


def _inspect_pair(
    cursor,
    *,
    reader_role: str,
    writer_role: str,
) -> BybitDemoPostgresControlRoleEvidenceV121:
    _require_schema_and_hardening(cursor)
    bootstrap_role = _current_user(cursor)
    reader = _inspect_role(
        cursor,
        role=reader_role,
        expected_table=_READER_TABLE,
        expected_sequence=_READER_SEQUENCE,
        bootstrap_role=bootstrap_role,
    )
    writer = _inspect_role(
        cursor,
        role=writer_role,
        expected_table=_WRITER_TABLE,
        expected_sequence=_WRITER_SEQUENCE,
        bootstrap_role=bootstrap_role,
    )
    append_ready = _trigger_ready(cursor, name=_APPEND_TRIGGER, truncate=False)
    truncate_ready = _trigger_ready(cursor, name=_TRUNCATE_TRIGGER, truncate=True)
    return BybitDemoPostgresControlRoleEvidenceV121(
        reader=reader,
        writer=writer,
        bootstrap_role=bootstrap_role,
        append_trigger_ready=append_ready,
        truncate_trigger_ready=truncate_ready,
        ready=reader.ready and writer.ready and append_ready and truncate_ready,
    )


def _inspect_role(
    cursor,
    *,
    role: str,
    expected_table: frozenset[str],
    expected_sequence: frozenset[str],
    bootstrap_role: str,
) -> BybitDemoPostgresControlRoleStateV121:
    attrs = _role_attributes(cursor, role)
    memberships = _memberships(cursor, role)
    schema_owner, table_owner, sequence_owner = _owners(cursor)
    table_privileges = _table_privileges(cursor, role)
    sequence_privileges = _sequence_privileges(cursor, role)
    database_create = _database_create(cursor, role)
    schema_usage = _schema_privilege(cursor, role, "USAGE")
    schema_create = _schema_privilege(cursor, role, "CREATE")
    function_execute = _function_execute(cursor, role)
    owner_memberships = tuple(
        sorted(
            owner
            for owner in {schema_owner, table_owner, sequence_owner, bootstrap_role}
            if owner != role and _is_member(cursor, member=role, target=owner)
        )
    )
    reasons: list[str] = []
    if role == bootstrap_role:
        reasons.append("BOOTSTRAP_ROLE_NOT_SEPARATED")
    for attribute, reason in (
        (attrs["rolsuper"], "SUPERUSER"),
        (attrs["rolcreatedb"], "CREATEDB"),
        (attrs["rolcreaterole"], "CREATEROLE"),
        (attrs["rolreplication"], "REPLICATION"),
        (attrs["rolbypassrls"], "BYPASSRLS"),
    ):
        if attribute:
            reasons.append(reason)
    if not attrs["rolcanlogin"]:
        reasons.append("NOT_LOGIN")
    for membership in memberships:
        reasons.append(f"ROLE_MEMBERSHIP:{membership}")
    if database_create:
        reasons.append("UNEXPECTED_DATABASE_CREATE")
    if not schema_usage:
        reasons.append("MISSING_SCHEMA_USAGE")
    if schema_create:
        reasons.append("UNEXPECTED_SCHEMA_CREATE")
    if schema_owner == role:
        reasons.append("OWNS_SCHEMA")
    if table_owner == role:
        reasons.append("OWNS_TABLE")
    if sequence_owner == role:
        reasons.append("OWNS_SEQUENCE")
    for owner in owner_memberships:
        reasons.append(f"MEMBER_OF_OWNER:{owner}")
    if table_privileges != expected_table:
        reasons.append("TABLE_PRIVILEGE_MISMATCH:" + ",".join(sorted(table_privileges)))
    if sequence_privileges != expected_sequence:
        reasons.append(
            "SEQUENCE_PRIVILEGE_MISMATCH:" + ",".join(sorted(sequence_privileges))
        )
    if function_execute:
        reasons.append("UNEXPECTED_MUTATION_FUNCTION_EXECUTE")
    return BybitDemoPostgresControlRoleStateV121(
        role=role,
        ready=not reasons,
        reasons=tuple(reasons),
        table_privileges=tuple(sorted(table_privileges)),
        sequence_privileges=tuple(sorted(sequence_privileges)),
        database_create=database_create,
        schema_usage=schema_usage,
        schema_create=schema_create,
        owns_schema=schema_owner == role,
        owns_table=table_owner == role,
        owns_sequence=sequence_owner == role,
        role_memberships=memberships,
        owner_role_memberships=owner_memberships,
        mutation_function_execute=function_execute,
        superuser=bool(attrs["rolsuper"]),
        createdb=bool(attrs["rolcreatedb"]),
        createrole=bool(attrs["rolcreaterole"]),
        replication=bool(attrs["rolreplication"]),
        bypassrls=bool(attrs["rolbypassrls"]),
        can_login=bool(attrs["rolcanlogin"]),
    )


__all__ = [
    "BybitDemoPostgresControlRoleEvidenceV121",
    "BybitDemoPostgresControlRoleStateV121",
    "PostgresBybitDemoControlJournalRolePolicyV121",
]
