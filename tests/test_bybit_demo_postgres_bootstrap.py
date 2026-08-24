from __future__ import annotations

import os

import pytest

from app.execution.bybit_demo_postgres_bootstrap import (
    BybitDemoPostgresBootstrapStatus,
    apply_bybit_demo_postgres_bootstrap,
    verify_bybit_demo_postgres_schema,
)

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_DEMO_BOOTSTRAP_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_DEMO_BOOTSTRAP_TEST_DSN is not configured",
)


def test_bootstrap_verify_apply_idempotency_and_locking() -> None:
    before = verify_bybit_demo_postgres_schema(_DSN)
    assert before.status is BybitDemoPostgresBootstrapStatus.SCHEMA_NOT_READY
    assert before.passed is False
    assert before.schema_mutation_performed is False
    assert len(before.migration_fingerprints) == 3
    assert [item.version for item in before.migration_fingerprints] == [
        "v119",
        "v120",
        "v121",
    ]
    assert all(len(item.sha256) == 64 for item in before.migration_fingerprints)

    with pytest.raises(ValueError, match="confirmation phrase"):
        apply_bybit_demo_postgres_bootstrap(
            _DSN,
            confirmation_phrase="WRONG",
        )

    applied = apply_bybit_demo_postgres_bootstrap(
        _DSN,
        confirmation_phrase="APPLY_BYBIT_DEMO_V119_V121",
    )
    assert applied.status is BybitDemoPostgresBootstrapStatus.APPLIED_AND_VERIFIED
    assert applied.passed is True
    assert applied.schema_mutation_performed is True
    assert applied.required_relations_present is True
    assert applied.append_only_triggers_present is True
    assert applied.bybit_credentials_required is False
    assert applied.bybit_order_writes_supported is False
    assert applied.live_mainnet_order_routing_allowed is False

    verified = verify_bybit_demo_postgres_schema(_DSN)
    assert verified.status is BybitDemoPostgresBootstrapStatus.VERIFIED_READY
    assert verified.passed is True
    assert verified.schema_mutation_performed is False
    assert verified.migration_fingerprints == applied.migration_fingerprints

    second = apply_bybit_demo_postgres_bootstrap(
        _DSN,
        confirmation_phrase="APPLY_BYBIT_DEMO_V119_V121",
    )
    assert second.status is BybitDemoPostgresBootstrapStatus.APPLIED_AND_VERIFIED
    assert second.migration_fingerprints == applied.migration_fingerprints

    with psycopg.connect(_DSN, autocommit=True) as lock_connection:
        locked = lock_connection.execute(
            "SELECT pg_try_advisory_lock(%s)",
            (119121,),
        ).fetchone()
        assert locked is not None and locked[0] is True
        with pytest.raises(RuntimeError, match="advisory lock is busy"):
            apply_bybit_demo_postgres_bootstrap(
                _DSN,
                confirmation_phrase="APPLY_BYBIT_DEMO_V119_V121",
            )
        released = lock_connection.execute(
            "SELECT pg_advisory_unlock(%s)",
            (119121,),
        ).fetchone()
        assert released is not None and released[0] is True
