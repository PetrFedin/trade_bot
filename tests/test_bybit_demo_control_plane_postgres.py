from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from app.execution.bybit_demo_connected_preflight import (
    BybitDemoConnectedPreflightResult,
    BybitDemoConnectedPreflightStatus,
)
from app.execution.bybit_demo_control_plane import (
    BybitDemoControlMode,
    PostgresBybitDemoControlPlane,
)
from app.execution.bybit_demo_postgres_bootstrap import (
    apply_bybit_demo_postgres_bootstrap,
)

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_DEMO_CONTROL_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_DEMO_CONTROL_TEST_DSN is not configured",
)
_NOW = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)


def _clean_preflight() -> BybitDemoConnectedPreflightResult:
    return BybitDemoConnectedPreflightResult(
        status=BybitDemoConnectedPreflightStatus.READY_FOR_MANUAL_OPERATOR_APPROVAL,
        reasons=(),
        margin_mode="REGULAR_MARGIN",
        unified_margin_status=6,
        positive_equity=True,
        positive_available_balance=True,
        usdt_wallet_visible=True,
        open_position_count=0,
        open_position_symbols=(),
        open_order_count=0,
        open_order_symbols=(),
        active_checkpoint_present=False,
        active_checkpoint_symbol=None,
        runtime_lease_present=False,
        required_relations_present=True,
        append_only_triggers_present=True,
        approval_record_count=0,
        provenance_record_count=0,
        terminal_record_count=0,
        read_only_api_key_verified=True,
        api_key_ip_binding_present=True,
    )


def test_control_plane_default_arm_halt_expiry_and_append_only() -> None:
    applied = apply_bybit_demo_postgres_bootstrap(
        _DSN,
        confirmation_phrase="APPLY_BYBIT_DEMO_V119_V122",
    )
    assert applied.passed is True

    plane = PostgresBybitDemoControlPlane(_DSN)
    initial = plane.read_decision(now=_NOW)
    assert initial.mode is BybitDemoControlMode.HALTED
    assert initial.new_entry_allowed is False
    assert initial.reasons == ("DEMO_CONTROL_NO_EVENT_DEFAULT_HALT",)

    armed = plane.arm_new_entries(
        _clean_preflight(),
        operator_id="integration-operator",
        reason="bounded connected Demo qualification",
        now=_NOW,
        preflight_observed_at=_NOW,
        ttl_seconds=120,
    )
    assert armed.event_kind == "ARM_NEW_ENTRIES"
    assert armed.armed_until == _NOW + timedelta(seconds=120)
    assert armed.preflight_record_sha256 is not None
    assert len(armed.preflight_record_sha256) == 64

    active = plane.read_decision(now=_NOW + timedelta(seconds=1))
    assert active.mode is BybitDemoControlMode.ARMED_NEW_ENTRIES
    assert active.new_entry_allowed is True
    assert active.latest_event_id == armed.event_id

    with psycopg.connect(_DSN, autocommit=True) as connection:
        with pytest.raises(psycopg.Error):
            connection.execute(
                """UPDATE astra_bybit_demo_control_event_v121
                   SET reason='tampered'
                   WHERE event_id=%s""",
                (armed.event_id,),
            )
        with pytest.raises(psycopg.Error):
            connection.execute(
                "DELETE FROM astra_bybit_demo_control_event_v121 WHERE event_id=%s",
                (armed.event_id,),
            )

    halted = plane.halt_new_entries(
        operator_id="integration-operator",
        reason="explicit operator halt",
        now=_NOW + timedelta(seconds=2),
    )
    assert halted.event_kind == "HALT_NEW_ENTRIES"
    stopped = plane.read_decision(now=_NOW + timedelta(seconds=3))
    assert stopped.mode is BybitDemoControlMode.HALTED
    assert stopped.reasons == ("DEMO_CONTROL_OPERATOR_HALT",)

    rearmed = plane.arm_new_entries(
        _clean_preflight(),
        operator_id="integration-operator",
        reason="expiry proof",
        now=_NOW + timedelta(seconds=4),
        preflight_observed_at=_NOW + timedelta(seconds=4),
        ttl_seconds=60,
    )
    expired = plane.read_decision(now=_NOW + timedelta(seconds=65))
    assert expired.mode is BybitDemoControlMode.HALTED
    assert expired.reasons == ("DEMO_CONTROL_ARM_EXPIRED",)
    assert expired.latest_event_id == rearmed.event_id


def test_arm_fails_when_runtime_lease_exists() -> None:
    apply_bybit_demo_postgres_bootstrap(
        _DSN,
        confirmation_phrase="APPLY_BYBIT_DEMO_V119_V122",
    )
    plane = PostgresBybitDemoControlPlane(_DSN)
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_demo_runtime_lease_v119(
                   lease_name, owner_token, created_time_ms, process_id, created_at
               ) VALUES ('CANONICAL_DEMO_TRADING_RUNTIME', %s, 1, 1, %s)""",
            ("b" * 64, _NOW),
        )
        try:
            with pytest.raises(RuntimeError, match="requires idle canonical runtime"):
                plane.arm_new_entries(
                    _clean_preflight(),
                    operator_id="integration-operator",
                    reason="must not race an active runtime",
                    now=_NOW + timedelta(seconds=10),
                    preflight_observed_at=_NOW + timedelta(seconds=10),
                )
        finally:
            connection.execute(
                "DELETE FROM astra_bybit_demo_runtime_lease_v119 WHERE owner_token=%s",
                ("b" * 64,),
            )
