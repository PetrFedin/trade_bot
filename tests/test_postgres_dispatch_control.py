from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL dispatch-control tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.runtime.paper_dispatch_control import (
    DispatchBlocked,
    DispatchControlMode,
    PostgresPaperDispatchControlStore,
)

NOW = datetime(2026, 9, 12, 16, 30, tzinfo=UTC)


def store() -> PostgresPaperDispatchControlStore:
    value = PostgresPaperDispatchControlStore(DSN)
    value.migrate()
    return value


def test_postgres_dispatch_control_is_durable_and_fail_closed_across_restart() -> None:
    first = store()
    first.halt(
        operator_id="pg-test",
        reason="reset to fail closed",
        occurred_at=NOW,
    )
    with pytest.raises(DispatchBlocked) as blocked:
        first.authorize(intent_id="pg-dispatch-intent", occurred_at=NOW)
    assert blocked.value.reasons == ("DISPATCH_CONTROL_HALTED",)

    armed = first.arm(
        operator_id="pg-test",
        reason="bounded qualification arm",
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert armed.mode is DispatchControlMode.ARMED
    authorization = first.authorize(
        intent_id="pg-dispatch-intent",
        occurred_at=NOW + timedelta(seconds=2),
    )
    assert authorization.control_version == armed.version

    reopened = PostgresPaperDispatchControlStore(DSN)
    persisted = reopened.current()
    assert persisted.mode is DispatchControlMode.ARMED
    assert persisted.version == armed.version

    halted = reopened.halt(
        operator_id="pg-test",
        reason="operator halt after authorization",
        occurred_at=NOW + timedelta(seconds=3),
    )
    assert halted.mode is DispatchControlMode.HALTED
    assert halted.version == armed.version + 1

    restarted = PostgresPaperDispatchControlStore(DSN)
    assert restarted.current().mode is DispatchControlMode.HALTED
    with pytest.raises(DispatchBlocked) as restart_blocked:
        restarted.authorize(
            intent_id="pg-dispatch-after-restart",
            occurred_at=NOW + timedelta(seconds=4),
        )
    assert restart_blocked.value.reasons == ("DISPATCH_CONTROL_HALTED",)


def test_postgres_dispatch_authorization_respects_arm_expiry() -> None:
    value = store()
    value.arm(
        operator_id="pg-expiry",
        reason="one-second arm",
        occurred_at=NOW + timedelta(minutes=1),
        ttl=timedelta(seconds=1),
    )
    with pytest.raises(DispatchBlocked) as blocked:
        value.authorize(
            intent_id="pg-expired-intent",
            occurred_at=NOW + timedelta(minutes=1, seconds=2),
        )
    assert blocked.value.reasons == ("DISPATCH_CONTROL_EXPIRED",)


def test_postgres_dispatch_event_journal_rejects_update_delete_and_truncate() -> None:
    value = store()
    value.arm(
        operator_id="pg-audit",
        reason="append-only qualification",
        occurred_at=NOW + timedelta(minutes=2),
    )
    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_paper_dispatch_control_events SET event_type='HALT'"
            )
        connection.rollback()

        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("DELETE FROM astra_paper_dispatch_control_events")
        connection.rollback()

        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("TRUNCATE astra_paper_dispatch_control_events")
        connection.rollback()

    assert any(
        event["event_type"] == "ARM"
        and event["payload"]["reason"] == "append-only qualification"
        for event in value.events()
    )
