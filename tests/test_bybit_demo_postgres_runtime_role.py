from __future__ import annotations

import pytest

from app.execution.bybit_demo_postgres_runtime_role import (
    BybitDemoPostgresRuntimeRoleEvidence,
    PostgresBybitDemoRuntimeRolePolicy,
    PostgresBybitDemoRuntimeRolePreflight,
)


def test_runtime_role_boundary_exposes_no_trading_or_role_creation_capability() -> None:
    for boundary in (PostgresBybitDemoRuntimeRolePolicy, PostgresBybitDemoRuntimeRolePreflight):
        assert boundary.automatic_role_creation_allowed is False
        assert boundary.runtime_ddl_allowed is False
        assert boundary.order_writes_supported is False
        assert boundary.live_mainnet_order_routing_allowed is False


def test_runtime_role_names_are_strict_and_bootstrap_must_be_distinct() -> None:
    with pytest.raises(ValueError, match="lowercase safe identifier"):
        PostgresBybitDemoRuntimeRolePreflight(
            "postgresql://example.invalid/db",
            expected_runtime_role="Unsafe-Role",
            expected_bootstrap_role="astra_bootstrap",
        )

    with pytest.raises(ValueError, match="must differ"):
        PostgresBybitDemoRuntimeRolePreflight(
            "postgresql://example.invalid/db",
            expected_runtime_role="astra_runtime",
            expected_bootstrap_role="astra_runtime",
        )


def test_runtime_role_evidence_serialization_contains_no_secret_fields() -> None:
    evidence = BybitDemoPostgresRuntimeRoleEvidence(
        runtime_role="astra_runtime",
        connected_role="astra_runtime",
        bootstrap_role="astra_bootstrap",
        ready=True,
        reasons=(),
        database_create=False,
        schema_usage=True,
        schema_create=False,
        runtime_owns_schema=False,
        runtime_owned_tables=(),
        role_memberships=(),
        owner_role_memberships=(),
        lease_privileges=("DELETE", "INSERT", "SELECT"),
        excursion_privileges=("DELETE", "INSERT", "SELECT", "UPDATE"),
        runtime_superuser=False,
        runtime_createdb=False,
        runtime_createrole=False,
        runtime_replication=False,
        runtime_bypassrls=False,
        runtime_can_login=True,
    )

    payload = evidence.to_dict()
    assert payload["ready"] is True
    assert payload["database_create"] is False
    assert payload["role_memberships"] == []
    assert payload["order_writes_supported"] is False
    assert payload["runtime_ddl_allowed"] is False
    assert payload["automatic_role_creation_allowed"] is False
    assert payload["live_mainnet_order_routing_allowed"] is False
    forbidden = {"dsn", "password", "secret", "api_key", "api_secret"}
    assert forbidden.isdisjoint(payload)
