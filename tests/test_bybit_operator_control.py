from __future__ import annotations

from datetime import UTC, datetime

from app.application.bybit_operator_control import (
    BybitOperatorMode,
    BybitOperatorSnapshot,
)


def _snapshot(mode: BybitOperatorMode) -> BybitOperatorSnapshot:
    return BybitOperatorSnapshot(
        mode=mode,
        generation=1,
        updated_at=datetime(2026, 8, 19, 19, 10, tzinfo=UTC),
        updated_by="SYSTEM",
        reason="test",
    )


def test_operator_modes_only_allow_new_entries_when_running() -> None:
    assert _snapshot(BybitOperatorMode.RUNNING).new_entries_allowed is True
    assert _snapshot(BybitOperatorMode.PAUSED).new_entries_allowed is False
    assert _snapshot(BybitOperatorMode.READ_ONLY).new_entries_allowed is False
    assert _snapshot(BybitOperatorMode.KILLED).new_entries_allowed is False


def test_operator_modes_never_disable_active_trade_safety_management() -> None:
    for mode in BybitOperatorMode:
        snapshot = _snapshot(mode)
        assert snapshot.active_trade_safety_management_allowed is True
        assert snapshot.live_mainnet_order_routing_allowed is False


def test_kill_and_read_only_flags_are_explicit() -> None:
    assert _snapshot(BybitOperatorMode.KILLED).kill_switch_engaged is True
    assert _snapshot(BybitOperatorMode.READ_ONLY).read_only_mode is True
    assert _snapshot(BybitOperatorMode.PAUSED).kill_switch_engaged is False
    assert _snapshot(BybitOperatorMode.RUNNING).read_only_mode is False
