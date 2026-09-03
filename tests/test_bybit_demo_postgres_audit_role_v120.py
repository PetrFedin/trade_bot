from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.execution.bybit_demo_postgres_audit_role import (
    PostgresBybitDemoAuditRolePolicy,
    PostgresBybitDemoAuditRolePreflight,
)
from app.execution.bybit_demo_postgres_runtime_lease import PostgresBybitDemoRuntimeLease

psycopg = pytest.importorskip("psycopg")
sql = pytest.importorskip("psycopg.sql")
conninfo = pytest.importorskip("psycopg.conninfo")
conninfo_to_dict = conninfo.conninfo_to_dict
make_conninfo = conninfo.make_conninfo

DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "Bybit Demo PostgreSQL v120 audit-role tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

ROOT = Path(__file__).resolve().parents[1]
V120_MIGRATIONS = (
    ROOT / "migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql",
    ROOT / "migrations/v120/002_bybit_demo_audit_truncate_hardening.sql",
)
APPROVAL_TABLE = "astra_bybit_demo_approved_entry_authorization_v120"
PROVENANCE_TABLE = "astra_bybit_demo_entry_provenance_v120"
TERMINAL_TABLE = "astra_bybit_demo_terminal_evidence_v120"
AUDIT_TABLES = (APPROVAL_TABLE, PROVENANCE_TABLE, TERMINAL_TABLE)
MUTATION_FUNCTION = "astra_reject_bybit_demo_audit_mutation_v120"


@dataclass(frozen=True)
class RuntimeRoleFixture:
    role: str
    password: str
    runtime_dsn: str
    bootstrap_role: str


def _safe_role(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _hex64() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _order_link_id(prefix: str) -> str:
    return f"ASTRA-DEMO-{prefix}-{uuid.uuid4().hex}"


def _dsn_for(*, role: str, password: str) -> str:
    values = conninfo_to_dict(DSN)
    values["user"] = role
    values["password"] = password
    return make_conninfo(**values)


def _create_login_role(role: str, password: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(password),
            )
        )


def _drop_role(role: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _bootstrap_role() -> str:
    with psycopg.connect(DSN) as connection:
        return connection.execute("SELECT current_user").fetchone()[0]


def _apply_sql(path: Path) -> None:
    with psycopg.connect(DSN) as connection:
        with connection.cursor() as cursor:
            cursor.execute(path.read_text(encoding="utf-8"))
        connection.commit()


def _apply_v120_twice() -> None:
    for _pass in range(2):
        for migration in V120_MIGRATIONS:
            _apply_sql(migration)


def _insert_approval(connection, *, order_link_id: str) -> None:
    connection.execute(
        f"""INSERT INTO {APPROVAL_TABLE}
            (entry_order_link_id, approval_id, source_snapshot_id,
             source_evidence_rank, source_market_rank, record_sha256,
             canonical_record, outcome_free, order_submission_supported,
             realized_pnl_storage_allowed, live_mainnet_order_routing_allowed,
             created_at)
            VALUES (%s, %s, %s, 1, 1, %s, '{{}}', true, false, false, false, now())""",
        (order_link_id, _hex64(), _hex64(), _hex64()),
    )


def _insert_provenance(connection, *, order_link_id: str) -> None:
    connection.execute(
        f"""INSERT INTO {PROVENANCE_TABLE}
            (entry_order_link_id, record_sha256, canonical_record, outcome_free,
             realized_pnl_storage_allowed, live_mainnet_order_routing_allowed,
             created_at)
            VALUES (%s, %s, '{{}}', true, false, false, now())""",
        (order_link_id, _hex64()),
    )


def _insert_terminal(connection, *, order_link_id: str) -> None:
    connection.execute(
        f"""INSERT INTO {TERMINAL_TABLE}
            (entry_order_link_id, checkpoint_revision, record_sha256,
             canonical_record, fully_reconciled_all_in, diagnostics_only,
             exit_threshold_retuning_allowed, live_mainnet_order_routing_allowed,
             created_at)
            VALUES (%s, %s, %s, '{{}}', true, true, false, false, now())""",
        (order_link_id, _hex64(), _hex64()),
    )


@pytest.fixture
def runtime_role() -> RuntimeRoleFixture:
    PostgresBybitDemoRuntimeLease(DSN).migrate()
    _apply_v120_twice()
    role = _safe_role("astra_c2a2_runtime")
    password = "astra-c2a2-runtime-test-only"
    _create_login_role(role, password)

    fixture = RuntimeRoleFixture(
        role=role,
        password=password,
        runtime_dsn=_dsn_for(role=role, password=password),
        bootstrap_role=_bootstrap_role(),
    )
    yield fixture
    _drop_role(role)


def test_policy_reconciles_exact_v119_plus_v120_runtime_privileges(
    runtime_role: RuntimeRoleFixture,
) -> None:
    policy = PostgresBybitDemoAuditRolePolicy(DSN)
    evidence = policy.reconcile(runtime_role=runtime_role.role)

    assert evidence.ready is True
    assert evidence.reasons == ()
    assert evidence.base_v119_ready is True
    assert evidence.base_v119_reasons == ()
    assert evidence.runtime_owned_tables == ()
    assert evidence.owner_role_memberships == ()
    assert evidence.approval_privileges == ("INSERT", "SELECT")
    assert evidence.provenance_privileges == ("INSERT", "SELECT")
    assert evidence.terminal_privileges == ("INSERT", "SELECT")
    assert evidence.truncate_hardened_tables == tuple(sorted(AUDIT_TABLES))
    assert evidence.mutation_function_execute is False
    assert evidence.automatic_role_creation_allowed is False
    assert evidence.runtime_ddl_allowed is False
    assert evidence.order_writes_supported is False
    assert evidence.live_mainnet_order_routing_allowed is False

    preflight = PostgresBybitDemoAuditRolePreflight(
        runtime_role.runtime_dsn,
        expected_runtime_role=runtime_role.role,
        expected_bootstrap_role=runtime_role.bootstrap_role,
    ).require_ready()
    assert preflight.ready is True
    assert preflight.connected_role == runtime_role.role


def test_runtime_can_only_insert_and_select_v120_audit_records(
    runtime_role: RuntimeRoleFixture,
) -> None:
    PostgresBybitDemoAuditRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)
    approval_id = _order_link_id("C2A2-APPROVAL")
    provenance_id = _order_link_id("C2A2-PROVENANCE")
    terminal_id = _order_link_id("C2A2-TERMINAL")

    with psycopg.connect(runtime_role.runtime_dsn, autocommit=True) as connection:
        _insert_approval(connection, order_link_id=approval_id)
        _insert_provenance(connection, order_link_id=provenance_id)
        _insert_terminal(connection, order_link_id=terminal_id)

        assert connection.execute(
            f"SELECT entry_order_link_id FROM {APPROVAL_TABLE} WHERE entry_order_link_id=%s",
            (approval_id,),
        ).fetchone()[0] == approval_id
        assert connection.execute(
            f"SELECT entry_order_link_id FROM {PROVENANCE_TABLE} WHERE entry_order_link_id=%s",
            (provenance_id,),
        ).fetchone()[0] == provenance_id
        assert connection.execute(
            f"SELECT entry_order_link_id FROM {TERMINAL_TABLE} WHERE entry_order_link_id=%s",
            (terminal_id,),
        ).fetchone()[0] == terminal_id


def test_append_only_triggers_reject_owner_update_delete_and_truncate(
    runtime_role: RuntimeRoleFixture,
) -> None:
    del runtime_role
    approval_id = _order_link_id("C2A2-OWNER")
    with psycopg.connect(DSN, autocommit=True) as connection:
        _insert_approval(connection, order_link_id=approval_id)

        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute(
                f"UPDATE {APPROVAL_TABLE} SET canonical_record=canonical_record "
                "WHERE entry_order_link_id=%s",
                (approval_id,),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute(
                f"DELETE FROM {APPROVAL_TABLE} WHERE entry_order_link_id=%s",
                (approval_id,),
            )
        for table in AUDIT_TABLES:
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                connection.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(table)))


def test_runtime_role_cannot_mutate_truncate_or_ddl_v120(
    runtime_role: RuntimeRoleFixture,
) -> None:
    PostgresBybitDemoAuditRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)

    with psycopg.connect(runtime_role.runtime_dsn, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(f"UPDATE {APPROVAL_TABLE} SET canonical_record='{{}}' WHERE false")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(f"DELETE FROM {PROVENANCE_TABLE} WHERE false")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(TERMINAL_TABLE)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                sql.SQL("ALTER TABLE {} ADD COLUMN forbidden_c2a2 boolean").format(
                    sql.Identifier(APPROVAL_TABLE)
                )
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(PROVENANCE_TABLE)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("CREATE TABLE public.astra_c2a2_forbidden_table (id integer)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("CREATE SCHEMA astra_c2a2_forbidden_schema")


def test_reconciliation_removes_v120_privilege_drift(runtime_role: RuntimeRoleFixture) -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            sql.SQL(
                "GRANT UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {} TO {}"
            ).format(sql.Identifier(APPROVAL_TABLE), sql.Identifier(runtime_role.role))
        )
        connection.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION {}() TO {}").format(
                sql.Identifier(MUTATION_FUNCTION),
                sql.Identifier(runtime_role.role),
            )
        )

    evidence = PostgresBybitDemoAuditRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)
    assert evidence.ready is True
    assert evidence.approval_privileges == ("INSERT", "SELECT")
    assert evidence.mutation_function_execute is False


def test_missing_truncate_hardening_fails_closed(runtime_role: RuntimeRoleFixture) -> None:
    trigger = "astra_bybit_demo_approval_no_truncate_v120"
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP TRIGGER {} ON {}").format(
                sql.Identifier(trigger),
                sql.Identifier(APPROVAL_TABLE),
            )
        )

    try:
        with pytest.raises(ValueError, match="V120_TRUNCATE_HARDENING_MISSING"):
            PostgresBybitDemoAuditRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)
    finally:
        _apply_sql(V120_MIGRATIONS[1])


def test_preflight_binds_actual_connection_to_expected_runtime_role(
    runtime_role: RuntimeRoleFixture,
) -> None:
    PostgresBybitDemoAuditRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)

    evidence = PostgresBybitDemoAuditRolePreflight(
        DSN,
        expected_runtime_role=runtime_role.role,
        expected_bootstrap_role=runtime_role.bootstrap_role,
    ).inspect()

    assert evidence.ready is False
    assert "BASE_V119:CONNECTED_ROLE_MISMATCH" in evidence.reasons


def test_v120_policy_is_idempotent(runtime_role: RuntimeRoleFixture) -> None:
    policy = PostgresBybitDemoAuditRolePolicy(DSN)
    first = policy.reconcile(runtime_role=runtime_role.role)
    second = policy.reconcile(runtime_role=runtime_role.role)

    assert first.ready is True
    assert second.ready is True
    assert first.approval_privileges == second.approval_privileges
    assert first.provenance_privileges == second.provenance_privileges
    assert first.terminal_privileges == second.terminal_privileges
    assert first.truncate_hardened_tables == second.truncate_hardened_tables
