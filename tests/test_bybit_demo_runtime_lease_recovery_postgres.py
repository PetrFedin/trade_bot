from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta

import pytest

from app.execution.bybit_demo_control_plane import (
    BybitDemoControlMode,
    PostgresBybitDemoControlPlane,
)
from app.execution.bybit_demo_postgres_bootstrap import apply_bybit_demo_postgres_bootstrap
from app.execution.bybit_demo_postgres_runtime_lease import PostgresBybitDemoRuntimeLease
from app.execution.bybit_demo_runtime_lease_recovery import (
    BybitDemoRuntimeLeaseRecoveryStatus,
    PostgresBybitDemoRuntimeLeaseRecovery,
)

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_DEMO_LEASE_RECOVERY_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_DEMO_LEASE_RECOVERY_TEST_DSN is not configured",
)


def test_explicit_halt_exact_identity_atomic_audit_and_idempotent_recovery() -> None:
    applied = apply_bybit_demo_postgres_bootstrap(
        _DSN,
        confirmation_phrase="APPLY_BYBIT_DEMO_V119_V123",
    )
    assert applied.passed is True

    lease_store = PostgresBybitDemoRuntimeLease(_DSN, clock_ms=lambda: 123_456)
    lease = lease_store.acquire()
    entry_order_link_id = "ASTRA-DEMO-E-LEASE-RECOVERY"
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_demo_active_excursion_v119(
                   checkpoint_name,
                   entry_order_link_id,
                   revision,
                   state_json,
                   diagnostics_only,
                   exit_threshold_retuning_allowed,
                   live_mainnet_order_routing_allowed,
                   created_at,
                   updated_at
               ) VALUES (
                   'ACTIVE', %s, %s, '{}'::jsonb, true, false, false, now(), now()
               )""",
            (entry_order_link_id, "a" * 64),
        )

    recovery = PostgresBybitDemoRuntimeLeaseRecovery(_DSN)
    before_halt = recovery.inspect()
    assert before_halt.status is BybitDemoRuntimeLeaseRecoveryStatus.BLOCKED
    assert before_halt.lease_present is True
    assert before_halt.explicit_operator_halt_present is False
    assert before_halt.active_checkpoint_present is True
    assert before_halt.lease_owner_sha256 == hashlib.sha256(
        lease.owner_token.encode("utf-8")
    ).hexdigest()
    assert before_halt.lease_owner_sha256 != lease.owner_token
    assert before_halt.active_checkpoint_entry_order_link_id_sha256 == hashlib.sha256(
        entry_order_link_id.encode("utf-8")
    ).hexdigest()

    control_time = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
    forged_time = control_time - timedelta(seconds=1)
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_demo_control_event_v121(
                   event_id, event_kind, operator_id, reason,
                   preflight_status, preflight_record_sha256,
                   preflight_canonical_record, preflight_observed_at,
                   armed_until, created_at, immutable_record,
                   order_submission_supported,
                   live_mainnet_order_routing_allowed
               ) VALUES (
                   %s, 'HALT_NEW_ENTRIES', %s, %s,
                   NULL, NULL, NULL, NULL, NULL, %s,
                   true, false, false
               )""",
            (
                "c" * 64,
                "forged-operator",
                "syntactically valid but cryptographically forged HALT",
                forged_time,
            ),
        )

    forged = recovery.inspect()
    assert forged.status is BybitDemoRuntimeLeaseRecoveryStatus.BLOCKED
    assert forged.explicit_operator_halt_present is False
    assert forged.latest_control_event_id is None
    assert forged.lease_owner_sha256 == before_halt.lease_owner_sha256

    control = PostgresBybitDemoControlPlane(_DSN)
    halt_receipt = control.halt_new_entries(
        operator_id="ops-recovery-test",
        reason="prove explicit HALT before orphan lease recovery",
        now=control_time,
    )
    decision = control.read_decision(now=control_time + timedelta(seconds=1))
    assert decision.mode is BybitDemoControlMode.HALTED
    assert decision.new_entry_allowed is False
    assert decision.latest_event_kind == "HALT_NEW_ENTRIES"

    ready = recovery.inspect()
    assert ready.status is BybitDemoRuntimeLeaseRecoveryStatus.RECOVERY_REQUIRED
    assert ready.recoverable is True
    assert ready.latest_control_event_id == halt_receipt.event_id
    assert ready.lease_owner_sha256 == before_halt.lease_owner_sha256

    with pytest.raises(ValueError, match="confirmation phrase"):
        recovery.recover(
            expected_lease_owner_sha256=ready.lease_owner_sha256,
            operator_id="ops-recovery-test",
            reason="wrong confirmation must not mutate",
            process_stop_evidence="service manager confirms old process stopped",
            confirmation_phrase="WRONG",
            now=control_time + timedelta(seconds=2),
        )
    assert lease_store.inspect().owner_token == lease.owner_token

    with pytest.raises(RuntimeError, match="fingerprint changed"):
        recovery.recover(
            expected_lease_owner_sha256="b" * 64,
            operator_id="ops-recovery-test",
            reason="wrong owner fingerprint must not mutate",
            process_stop_evidence="service manager confirms old process stopped",
            confirmation_phrase="RECOVER_BYBIT_DEMO_RUNTIME_LEASE",
            now=control_time + timedelta(seconds=3),
        )
    assert lease_store.inspect().owner_token == lease.owner_token

    receipt = recovery.recover(
        expected_lease_owner_sha256=ready.lease_owner_sha256,
        operator_id="ops-recovery-test",
        reason="previous supervisor process is proven stopped",
        process_stop_evidence="systemd unit stopped and old container identity removed",
        confirmation_phrase="RECOVER_BYBIT_DEMO_RUNTIME_LEASE",
        now=control_time + timedelta(seconds=4),
    )
    assert receipt.status is BybitDemoRuntimeLeaseRecoveryStatus.RECOVERED
    assert receipt.lease_owner_sha256 == ready.lease_owner_sha256
    assert receipt.control_event_id == halt_receipt.event_id
    assert receipt.active_checkpoint_present is True
    assert receipt.idempotent_existing_recovery is False
    assert receipt.automatic_recovery_allowed is False
    assert receipt.automatic_stale_takeover_allowed is False
    assert receipt.order_writes_supported is False
    assert receipt.live_mainnet_order_routing_allowed is False

    with pytest.raises(FileNotFoundError):
        lease_store.inspect()

    after = recovery.inspect()
    assert after.status is BybitDemoRuntimeLeaseRecoveryStatus.NO_LEASE_PRESENT
    assert after.active_checkpoint_present is True
    assert after.explicit_operator_halt_present is True

    repeated = recovery.recover(
        expected_lease_owner_sha256=ready.lease_owner_sha256,
        operator_id="another-operator-does-not-rewrite-history",
        reason="retry after caller lost the first response",
        process_stop_evidence="same external incident remains resolved",
        confirmation_phrase="RECOVER_BYBIT_DEMO_RUNTIME_LEASE",
        now=control_time + timedelta(seconds=5),
    )
    assert repeated.status is BybitDemoRuntimeLeaseRecoveryStatus.ALREADY_RECOVERED
    assert repeated.recovery_id == receipt.recovery_id
    assert repeated.idempotent_existing_recovery is True

    with psycopg.connect(_DSN, autocommit=True) as connection:
        row = connection.execute(
            """SELECT count(*), bool_and(immutable_record),
                      bool_and(NOT order_writes_supported),
                      bool_and(NOT automatic_stale_takeover_allowed),
                      bool_and(NOT live_mainnet_order_routing_allowed)
               FROM astra_bybit_demo_runtime_lease_recovery_v123"""
        ).fetchone()
        assert row == (1, True, True, True, True)
        checkpoint = connection.execute(
            """SELECT entry_order_link_id
               FROM astra_bybit_demo_active_excursion_v119
               WHERE checkpoint_name='ACTIVE'"""
        ).fetchone()
        assert checkpoint is not None and checkpoint[0] == entry_order_link_id

    with pytest.raises(psycopg.Error, match="append-only"):
        with psycopg.connect(_DSN, autocommit=True) as connection:
            connection.execute(
                "UPDATE astra_bybit_demo_runtime_lease_recovery_v123 SET reason='mutated'"
            )
    with pytest.raises(psycopg.Error, match="append-only"):
        with psycopg.connect(_DSN, autocommit=True) as connection:
            connection.execute("TRUNCATE astra_bybit_demo_runtime_lease_recovery_v123")
    with pytest.raises(psycopg.Error, match="append-only"):
        with psycopg.connect(_DSN, autocommit=True) as connection:
            connection.execute("TRUNCATE astra_bybit_demo_control_event_v121")

    still_halted = control.read_decision(now=control_time + timedelta(seconds=6))
    assert still_halted.mode is BybitDemoControlMode.HALTED
    assert still_halted.new_entry_allowed is False

    replacement = lease_store.acquire()
    assert replacement.owner_token != lease.owner_token
    lease_store.release(owner_token=replacement.owner_token)
