from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.execution.bybit_demo_operational_zone_binding import (
    bind_bybit_demo_account_identity,
    bind_bybit_demo_database_dsn,
    build_bybit_demo_operational_zone_binding,
)
from app.execution.bybit_demo_same_account import BybitDemoApiAccountIdentity

_SECRET = "zone-binding-secret-that-is-long-enough-123456"
_GIT_SHA = "a" * 40


def test_database_binding_ignores_credentials_but_binds_endpoint_and_database() -> None:
    first = bind_bybit_demo_database_dsn(
        "postgresql://user-a:password-a@db.example.com:5432/astra_demo?sslmode=require",
        binding_secret=_SECRET,
    )
    rotated = bind_bybit_demo_database_dsn(
        "postgresql://user-b:password-b@DB.EXAMPLE.COM:5432/astra_demo?sslmode=require",
        binding_secret=_SECRET,
    )
    other_database = bind_bybit_demo_database_dsn(
        "postgresql://user-b:password-b@db.example.com:5432/other_demo?sslmode=require",
        binding_secret=_SECRET,
    )
    other_host = bind_bybit_demo_database_dsn(
        "postgresql://user-b:password-b@other.example.com:5432/astra_demo?sslmode=require",
        binding_secret=_SECRET,
    )

    assert first == rotated
    assert first != other_database
    assert first != other_host
    assert len(first) == 64


def test_account_binding_is_stable_for_same_authenticated_account_identity() -> None:
    identity = BybitDemoApiAccountIdentity(
        user_id=123456,
        parent_uid=0,
        is_master=True,
    )
    same = BybitDemoApiAccountIdentity(
        user_id=123456,
        parent_uid=0,
        is_master=True,
    )
    different = BybitDemoApiAccountIdentity(
        user_id=654321,
        parent_uid=0,
        is_master=True,
    )

    first = bind_bybit_demo_account_identity(identity, binding_secret=_SECRET)
    second = bind_bybit_demo_account_identity(same, binding_secret=_SECRET)
    other = bind_bybit_demo_account_identity(different, binding_secret=_SECRET)

    assert first == second
    assert first != other
    assert len(first) == 64


def test_binding_secret_rotation_is_detectable_by_marker_and_resource_tokens() -> None:
    database = "postgresql://user:password@db.example.com/astra_demo"
    first = bind_bybit_demo_database_dsn(database, binding_secret=_SECRET)
    second = bind_bybit_demo_database_dsn(
        database,
        binding_secret="another-zone-binding-secret-that-is-long-enough-987654",
    )

    assert first != second


def test_database_only_sidecar_is_sanitized_and_self_describing() -> None:
    dsn = "postgresql://operator:super-secret@db.example.com:5432/astra_demo"
    result = build_bybit_demo_operational_zone_binding(
        producer="session_start",
        git_sha=_GIT_SHA,
        binding_secret=_SECRET,
        database_dsn=dsn,
        observed_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
    )

    payload = result.to_payload()
    serialized = json.dumps(payload, sort_keys=True)
    assert payload["producer"] == "session_start"
    assert payload["database_binding_present"] is True
    assert payload["demo_account_binding_present"] is False
    assert payload["binding_key_marker_sha256"]
    assert "operator" not in serialized
    assert "super-secret" not in serialized
    assert "db.example.com" not in serialized
    assert "astra_demo" not in serialized
    assert _SECRET not in serialized


def test_binding_requires_high_entropy_separate_secret_and_known_producer() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        bind_bybit_demo_database_dsn(
            "postgresql://user:password@db.example.com/astra_demo",
            binding_secret="too-short",
        )

    with pytest.raises(ValueError, match="producer"):
        build_bybit_demo_operational_zone_binding(
            producer="unknown",
            git_sha=_GIT_SHA,
            binding_secret=_SECRET,
            database_dsn="postgresql://user:password@db.example.com/astra_demo",
        )


def test_invalid_database_identity_fails_closed() -> None:
    with pytest.raises(ValueError, match="host"):
        bind_bybit_demo_database_dsn(
            "postgresql:///astra_demo",
            binding_secret=_SECRET,
        )

    with pytest.raises(ValueError, match="database"):
        bind_bybit_demo_database_dsn(
            "postgresql://user:password@db.example.com/",
            binding_secret=_SECRET,
        )
