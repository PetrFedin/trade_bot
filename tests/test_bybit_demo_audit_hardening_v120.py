from __future__ import annotations

import hashlib
from pathlib import Path

from app.execution.bybit_demo_postgres_audit_role import (
    PostgresBybitDemoAuditRolePolicy,
    PostgresBybitDemoAuditRolePreflight,
)

ROOT = Path(__file__).resolve().parents[1]
FROZEN_V120 = ROOT / "migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql"
HARDENING_V120 = ROOT / "migrations/v120/002_bybit_demo_audit_truncate_hardening.sql"
FROZEN_GIT_BLOB_SHA1 = "b337ef19dc7da4a3fcbc0a11a8d6d7d85dff3b00"
FROZEN_SHA256 = "613a21fba9142f34141cb7d6c81938b175250fb4dbda9bd37726e5627df094cf"


def _git_blob_sha1(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode()
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def test_frozen_v120_migration_is_byte_preserved() -> None:
    payload = FROZEN_V120.read_bytes()

    assert _git_blob_sha1(payload) == FROZEN_GIT_BLOB_SHA1
    assert hashlib.sha256(payload).hexdigest() == FROZEN_SHA256


def test_forward_hardening_is_statement_level_truncate_only_extension() -> None:
    original = FROZEN_V120.read_text(encoding="utf-8")
    hardening = HARDENING_V120.read_text(encoding="utf-8")

    assert "BEFORE UPDATE OR DELETE" in original
    assert "BEFORE TRUNCATE" not in original
    assert hardening.count("BEFORE TRUNCATE ON") == 3
    assert hardening.count("FOR EACH STATEMENT EXECUTE FUNCTION") == 3
    assert "ALTER TABLE" not in hardening
    assert "DROP TABLE" not in hardening


def test_c2a2_role_boundaries_have_no_trading_capability() -> None:
    for cls in (PostgresBybitDemoAuditRolePolicy, PostgresBybitDemoAuditRolePreflight):
        assert cls.automatic_role_creation_allowed is False
        assert cls.runtime_ddl_allowed is False
        assert cls.order_writes_supported is False
        assert cls.live_mainnet_order_routing_allowed is False
