from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL Bybit operator tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.application.bybit_operator_control import (
    BybitOperatorMode,
    PostgresBybitOperatorControl,
)

NOW = datetime(2026, 8, 19, 19, 15, tzinfo=UTC)


@pytest.fixture()
def control() -> PostgresBybitOperatorControl:
    value = PostgresBybitOperatorControl(DSN)
    value.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("TRUNCATE astra_bybit_operator_actions")
        connection.execute(
            """UPDATE astra_bybit_operator_state
            SET mode='PAUSED', generation=1, updated_at=%s,
                updated_by='SYSTEM', reason='TEST_FAIL_CLOSED_RESET'
            WHERE singleton=TRUE""",
            (NOW,),
        )
    return value


def test_operator_state_starts_fail_closed_and_persists_across_processes(
    control: PostgresBybitOperatorControl,
) -> None:
    initial = control.inspect()
    assert initial.mode is BybitOperatorMode.PAUSED
    assert initial.new_entries_allowed is False
    assert initial.active_trade_safety_management_allowed is True

    running = control.resume(
        actor="operator-a",
        reason="startup checks completed",
        occurred_at=NOW + timedelta(seconds=1),
        action_id="resume-1",
    )
    assert running.mode is BybitOperatorMode.RUNNING
    assert running.generation == 2
    assert running.new_entries_allowed is True

    reopened = PostgresBybitOperatorControl(DSN).inspect()
    assert reopened == running


def test_kill_requires_explicit_clear_before_resume(
    control: PostgresBybitOperatorControl,
) -> None:
    control.resume(
        actor="operator-a",
        reason="start",
        occurred_at=NOW + timedelta(seconds=1),
        action_id="resume-before-kill",
    )
    killed = control.kill(
        actor="operator-a",
        reason="incident",
        occurred_at=NOW + timedelta(seconds=2),
        action_id="kill-1",
    )
    assert killed.mode is BybitOperatorMode.KILLED
    assert killed.kill_switch_engaged is True
    assert killed.new_entries_allowed is False
    assert killed.active_trade_safety_management_allowed is True

    with pytest.raises(RuntimeError, match="KILLED->RUNNING"):
        control.resume(
            actor="operator-a",
            reason="unsafe direct resume",
            occurred_at=NOW + timedelta(seconds=3),
            action_id="resume-after-kill",
        )

    cleared = control.clear_kill(
        actor="operator-b",
        reason="incident reviewed and broker state reconciled",
        occurred_at=NOW + timedelta(seconds=4),
        action_id="clear-kill-1",
    )
    assert cleared.mode is BybitOperatorMode.PAUSED
    assert cleared.kill_switch_engaged is False
    assert cleared.new_entries_allowed is False

    running = control.resume(
        actor="operator-b",
        reason="separate resume after clear",
        occurred_at=NOW + timedelta(seconds=5),
        action_id="resume-after-clear",
    )
    assert running.mode is BybitOperatorMode.RUNNING


def test_read_only_and_pause_are_durable_new_entry_blockers(
    control: PostgresBybitOperatorControl,
) -> None:
    control.resume(
        actor="operator-a",
        reason="start",
        occurred_at=NOW + timedelta(seconds=1),
        action_id="resume-ro",
    )
    read_only = control.enter_read_only(
        actor="operator-a",
        reason="broker investigation",
        occurred_at=NOW + timedelta(seconds=2),
        action_id="read-only-1",
    )
    assert read_only.mode is BybitOperatorMode.READ_ONLY
    assert read_only.read_only_mode is True
    assert read_only.new_entries_allowed is False
    assert read_only.active_trade_safety_management_allowed is True

    paused = control.pause(
        actor="operator-a",
        reason="maintenance",
        occurred_at=NOW + timedelta(seconds=3),
        action_id="pause-1",
    )
    assert paused.mode is BybitOperatorMode.PAUSED
    assert paused.new_entries_allowed is False


def test_operator_actions_are_append_only_and_ordered_by_generation(
    control: PostgresBybitOperatorControl,
) -> None:
    control.resume(
        actor="operator-a",
        reason="start",
        occurred_at=NOW + timedelta(seconds=1),
        action_id="history-resume",
    )
    control.pause(
        actor="operator-b",
        reason="review",
        occurred_at=NOW + timedelta(seconds=2),
        action_id="history-pause",
    )

    history = control.history()
    assert [action.action_id for action in history] == ["history-pause", "history-resume"]
    assert [action.generation for action in history] == [3, 2]
    assert history[0].from_mode is BybitOperatorMode.RUNNING
    assert history[0].to_mode is BybitOperatorMode.PAUSED

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_bybit_operator_actions SET reason='tampered' "
                "WHERE action_id='history-resume'"
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "DELETE FROM astra_bybit_operator_actions WHERE action_id='history-resume'"
            )
        connection.rollback()
