from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import pytest

from app.execution.bybit_demo_postgres_runtime_lease import PostgresBybitDemoRuntimeLease
from app.execution.bybit_demo_postgres_runtime_role import (
    PostgresBybitDemoRuntimeRolePolicy,
    PostgresBybitDemoRuntimeRolePreflight,
)

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "Bybit Demo PostgreSQL runtime-role tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

LEASE_NAME = "CANONICAL_DEMO_TRADING_RUNTIME"
LEASE_TABLE = "astra_bybit_demo_runtime_lease_v119"


@dataclass(frozen=True)
class RuntimeRoleFixture:
    role: str
    password: str
    runtime_dsn: str
    bootstrap_role: str


def _safe_role(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _dsn_for(*, role: str, password: str) -> str:
    values = conninfo_to_dict(DSN)
    values["user"] = role
    values["password"] = password
    return make_conninfo(**values)


def _create_login_role(role: str, password: str, *, superuser: bool = False) -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        attributes = sql.SQL("SUPERUSER") if superuser else sql.SQL("NOSUPERUSER")
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN {} PASSWORD {}").format(
                sql.Identifier(role),
                attributes,
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


@pytest.fixture
def runtime_role() -> RuntimeRoleFixture:
    PostgresBybitDemoRuntimeLease(DSN).migrate()
    role = _safe_role("astra_c2a1_runtime")
    password = "astra-c2a1-runtime-test-only"
    _create_login_role(role, password)

    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "DELETE FROM astra_bybit_demo_runtime_lease_v119 WHERE lease_name=%s",
            (LEASE_NAME,),
        )
        connection.execute("DELETE FROM astra_bybit_demo_active_excursion_v119")

    fixture = RuntimeRoleFixture(
        role=role,
        password=password,
        runtime_dsn=_dsn_for(role=role, password=password),
        bootstrap_role=_bootstrap_role(),
    )
    yield fixture

    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "DELETE FROM astra_bybit_demo_runtime_lease_v119 WHERE lease_name=%s",
            (LEASE_NAME,),
        )
        connection.execute("DELETE FROM astra_bybit_demo_active_excursion_v119")
    _drop_role(role)


def test_policy_reconciles_exact_v119_runtime_privileges(runtime_role: RuntimeRoleFixture) -> None:
    policy = PostgresBybitDemoRuntimeRolePolicy(DSN)
    evidence = policy.reconcile(runtime_role=runtime_role.role)

    assert evidence.ready is True
    assert evidence.reasons == ()
    assert evidence.runtime_role == runtime_role.role
    assert evidence.bootstrap_role == runtime_role.bootstrap_role
    assert evidence.database_create is False
    assert evidence.schema_usage is True
    assert evidence.schema_create is False
    assert evidence.runtime_owns_schema is False
    assert evidence.runtime_owned_tables == ()
    assert evidence.role_memberships == ()
    assert evidence.owner_role_memberships == ()
    assert evidence.lease_privileges == ("DELETE", "INSERT", "SELECT")
    assert evidence.excursion_privileges == ("DELETE", "INSERT", "SELECT", "UPDATE")
    assert evidence.runtime_superuser is False
    assert evidence.runtime_createdb is False
    assert evidence.runtime_createrole is False
    assert evidence.runtime_replication is False
    assert evidence.runtime_bypassrls is False
    assert evidence.runtime_can_login is True
    assert evidence.automatic_role_creation_allowed is False
    assert evidence.runtime_ddl_allowed is False
    assert evidence.order_writes_supported is False
    assert evidence.live_mainnet_order_routing_allowed is False

    preflight = PostgresBybitDemoRuntimeRolePreflight(
        runtime_role.runtime_dsn,
        expected_runtime_role=runtime_role.role,
        expected_bootstrap_role=runtime_role.bootstrap_role,
    ).require_ready()
    assert preflight.ready is True
    assert preflight.connected_role == runtime_role.role


def test_canonical_runtime_lease_works_under_non_owner_role(
    runtime_role: RuntimeRoleFixture,
) -> None:
    PostgresBybitDemoRuntimeRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)
    store = PostgresBybitDemoRuntimeLease(runtime_role.runtime_dsn, clock_ms=lambda: 123)

    acquired = store.acquire()
    assert acquired.created_time_ms == 123
    assert store.inspect() == acquired

    with pytest.raises(RuntimeError, match="ownership changed"):
        store.release(owner_token="f" * 64)

    assert store.inspect() == acquired
    store.release(owner_token=acquired.owner_token)
    with pytest.raises(FileNotFoundError, match="does not exist"):
        store.inspect()


def test_active_excursion_exact_dml_works_without_strategy_adapter(
    runtime_role: RuntimeRoleFixture,
) -> None:
    PostgresBybitDemoRuntimeRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)

    with psycopg.connect(runtime_role.runtime_dsn, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_demo_active_excursion_v119
               (checkpoint_name, entry_order_link_id, revision, state_json,
                diagnostics_only, exit_threshold_retuning_allowed,
                live_mainnet_order_routing_allowed, created_at, updated_at)
               VALUES ('ACTIVE', 'ASTRA-DEMO-C2A1', %s, '{}'::jsonb,
                       true, false, false, now(), now())""",
            ("a" * 64,),
        )
        row = connection.execute(
            """SELECT revision FROM astra_bybit_demo_active_excursion_v119
               WHERE checkpoint_name='ACTIVE'"""
        ).fetchone()
        assert row[0] == "a" * 64

        connection.execute(
            """UPDATE astra_bybit_demo_active_excursion_v119
               SET revision=%s, updated_at=now()
               WHERE checkpoint_name='ACTIVE' AND revision=%s""",
            ("b" * 64, "a" * 64),
        )
        deleted = connection.execute(
            """DELETE FROM astra_bybit_demo_active_excursion_v119
               WHERE checkpoint_name='ACTIVE' AND revision=%s
               RETURNING entry_order_link_id""",
            ("b" * 64,),
        ).fetchone()
        assert deleted[0] == "ASTRA-DEMO-C2A1"


def test_runtime_role_cannot_truncate_alter_drop_or_create(
    runtime_role: RuntimeRoleFixture,
) -> None:
    PostgresBybitDemoRuntimeRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)

    with psycopg.connect(runtime_role.runtime_dsn, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(LEASE_TABLE)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                sql.SQL("ALTER TABLE {} ADD COLUMN forbidden_c2a1 boolean").format(
                    sql.Identifier(LEASE_TABLE)
                )
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(LEASE_TABLE)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "CREATE TABLE public.astra_c2a1_forbidden_runtime_table (id integer)"
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("CREATE SCHEMA astra_c2a1_forbidden_runtime_schema")


def test_direct_excess_privilege_is_removed_by_reconciliation(
    runtime_role: RuntimeRoleFixture,
) -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            sql.SQL("GRANT UPDATE, TRUNCATE ON TABLE {} TO {}").format(
                sql.Identifier(LEASE_TABLE),
                sql.Identifier(runtime_role.role),
            )
        )

    evidence = PostgresBybitDemoRuntimeRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)
    assert evidence.ready is True
    assert evidence.lease_privileges == ("DELETE", "INSERT", "SELECT")


def test_explicit_role_membership_is_rejected_before_grants(
    runtime_role: RuntimeRoleFixture,
) -> None:
    group = _safe_role("astra_c2a1_group")
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(group)))
        connection.execute(
            sql.SQL("GRANT TRUNCATE ON TABLE {} TO {}").format(
                sql.Identifier(LEASE_TABLE),
                sql.Identifier(group),
            )
        )
        connection.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(group),
                sql.Identifier(runtime_role.role),
            )
        )

    try:
        with pytest.raises(ValueError, match="RUNTIME_ROLE_HAS_MEMBERSHIP"):
            PostgresBybitDemoRuntimeRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)
    finally:
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(
                sql.SQL("REVOKE {} FROM {}").format(
                    sql.Identifier(group),
                    sql.Identifier(runtime_role.role),
                )
            )
        _drop_role(group)


def test_superuser_runtime_role_is_rejected_before_grants() -> None:
    PostgresBybitDemoRuntimeLease(DSN).migrate()
    role = _safe_role("astra_c2a1_super")
    password = "astra-c2a1-super-test-only"
    _create_login_role(role, password, superuser=True)

    try:
        with pytest.raises(ValueError, match="RUNTIME_ROLE_SUPERUSER"):
            PostgresBybitDemoRuntimeRolePolicy(DSN).reconcile(runtime_role=role)
    finally:
        _drop_role(role)


def test_preflight_binds_actual_connection_to_expected_runtime_role(
    runtime_role: RuntimeRoleFixture,
) -> None:
    PostgresBybitDemoRuntimeRolePolicy(DSN).reconcile(runtime_role=runtime_role.role)

    evidence = PostgresBybitDemoRuntimeRolePreflight(
        DSN,
        expected_runtime_role=runtime_role.role,
        expected_bootstrap_role=runtime_role.bootstrap_role,
    ).inspect()

    assert evidence.ready is False
    assert evidence.connected_role == runtime_role.bootstrap_role
    assert "CONNECTED_ROLE_MISMATCH" in evidence.reasons


def test_policy_is_idempotent_and_never_creates_roles(runtime_role: RuntimeRoleFixture) -> None:
    policy = PostgresBybitDemoRuntimeRolePolicy(DSN)
    first = policy.reconcile(runtime_role=runtime_role.role)
    second = policy.reconcile(runtime_role=runtime_role.role)

    assert first.ready is True
    assert second.ready is True
    assert first.lease_privileges == second.lease_privileges
    assert first.excursion_privileges == second.excursion_privileges
    assert policy.automatic_role_creation_allowed is False
    assert policy.runtime_ddl_allowed is False
    assert policy.order_writes_supported is False
    assert policy.live_mainnet_order_routing_allowed is False
