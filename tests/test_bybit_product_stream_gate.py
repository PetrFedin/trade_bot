from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.execution.bybit_startup_reconciliation import (
    BybitStartupReconciliationResult,
    BybitStartupReconciliationStatus,
)
from app.runtime.bybit_product_config import BybitProductConfig
from app.runtime.bybit_product_service import (
    BybitProductServiceStatus,
    run_bybit_product_service,
)


@dataclass(frozen=True)
class _Snapshot:
    healthy: bool
    reconciliation_required: bool = False
    reconciliation_token: int = 0
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _OperatorSnapshot:
    new_entries_allowed: bool = True
    active_trade_safety_management_allowed: bool = True
    live_mainnet_order_routing_allowed: bool = False


class _OperatorControl:
    live_mainnet_order_routing_allowed = False
    active_trade_safety_management_allowed = True

    def inspect(self) -> _OperatorSnapshot:
        return _OperatorSnapshot()


class _Monitor:
    live_mainnet_order_routing_allowed = False

    def __init__(self, snapshot: _Snapshot, *, acknowledge: bool = True) -> None:
        self.current = snapshot
        self.acknowledge = acknowledge
        self.started = False
        self.stopped = False
        self.waits = 0
        self.ack_tokens: list[int] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def snapshot(self) -> _Snapshot:
        return self.current

    def acknowledge_reconciliation(self, *, token: int) -> bool:
        self.ack_tokens.append(token)
        if self.acknowledge:
            self.current = _Snapshot(healthy=self.current.healthy)
        return self.acknowledge

    def wait(self, _timeout_seconds: float) -> None:
        self.waits += 1


class _Reconciler:
    live_mainnet_order_routing_allowed = False

    def __init__(self, results: list[BybitStartupReconciliationResult]) -> None:
        self.results = results
        self.calls = 0

    def run(self) -> BybitStartupReconciliationResult:
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[index]


@dataclass
class _CycleResult:
    live_mainnet_order_routing_allowed: bool = False
    same_invocation_additional_entry_allowed: bool = False


class _Executor:
    live_mainnet_order_routing_allowed = False
    demo_order_writes_enabled = False

    def __init__(self, *, active: bool) -> None:
        self.active = active
        self.calls = 0
        self.active_reads = 0

    def has_active_trade(self) -> bool:
        self.active_reads += 1
        return self.active

    def run_once(self) -> object:
        self.calls += 1
        return _CycleResult()


def _config() -> BybitProductConfig:
    return BybitProductConfig.from_env(
        {
            "ASTRA_ENV": "demo",
            "ASTRA_BROKER": "bybit",
            "BYBIT_API_KEY": "demo-key",
            "BYBIT_API_SECRET": "demo-secret",
            "DATABASE_URL": "postgresql://astra:secret@db.example/astra",
            "TRADING_WRITES_ENABLED": "false",
            "MAINNET_ENABLED": "false",
        }
    )


def _startup(status: BybitStartupReconciliationStatus) -> BybitStartupReconciliationResult:
    return BybitStartupReconciliationResult(
        status=status,
        reasons=(status.value,),
        checkpoint=None,
        active_positions=(),
        open_orders=(),
        next_entry_allowed=status is BybitStartupReconciliationStatus.READY_FOR_ENTRY,
        management_allowed=status is BybitStartupReconciliationStatus.RESUME_MANAGEMENT,
        terminal_recovery_required=(
            status is BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED
        ),
        broker_truth_complete=status is not BybitStartupReconciliationStatus.BLOCKED,
    )


def test_flat_runtime_pauses_entries_while_private_stream_is_unhealthy() -> None:
    monitor = _Monitor(_Snapshot(healthy=False))
    executor = _Executor(active=False)

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=_Reconciler(
            [_startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)]
        ),
        cycle_executor=executor,
        operator_control=_OperatorControl(),
        private_stream_monitor=monitor,
        stop_requested=lambda: monitor.waits > 0,
        sleep_fn=lambda _: None,
    )

    assert result.status is BybitProductServiceStatus.STOPPED
    assert executor.calls == 0
    assert executor.active_reads == 1
    assert monitor.waits == 1
    assert monitor.started is True
    assert monitor.stopped is True


def test_active_trade_continues_rest_management_when_stream_is_unhealthy() -> None:
    monitor = _Monitor(_Snapshot(healthy=False))
    executor = _Executor(active=True)

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=_Reconciler(
            [_startup(BybitStartupReconciliationStatus.RESUME_MANAGEMENT)]
        ),
        cycle_executor=executor,
        operator_control=_OperatorControl(),
        private_stream_monitor=monitor,
        stop_requested=lambda: False,
        sleep_fn=lambda _: None,
        max_cycles=1,
    )

    assert result.status is BybitProductServiceStatus.STOPPED
    assert result.completed_cycles == 1
    assert executor.calls == 1
    assert executor.active_reads == 1
    assert monitor.stopped is True


def test_stream_event_requires_fresh_rest_reconciliation_before_cycle() -> None:
    monitor = _Monitor(
        _Snapshot(healthy=True, reconciliation_required=True, reconciliation_token=7)
    )
    reconciler = _Reconciler(
        [
            _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY),
            _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY),
        ]
    )
    executor = _Executor(active=False)

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=reconciler,
        cycle_executor=executor,
        operator_control=_OperatorControl(),
        private_stream_monitor=monitor,
        stop_requested=lambda: False,
        sleep_fn=lambda _: None,
        max_cycles=1,
    )

    assert result.status is BybitProductServiceStatus.STOPPED
    assert reconciler.calls == 2
    assert monitor.ack_tokens == [7]
    assert executor.calls == 1


def test_blocked_stream_reconciliation_stops_before_trading_cycle() -> None:
    monitor = _Monitor(
        _Snapshot(healthy=True, reconciliation_required=True, reconciliation_token=9)
    )
    reconciler = _Reconciler(
        [
            _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY),
            _startup(BybitStartupReconciliationStatus.BLOCKED),
        ]
    )
    executor = _Executor(active=False)

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=reconciler,
        cycle_executor=executor,
        operator_control=_OperatorControl(),
        private_stream_monitor=monitor,
        stop_requested=lambda: False,
        sleep_fn=lambda _: None,
        max_cycles=1,
    )

    assert result.status is BybitProductServiceStatus.CYCLE_FAILED
    assert result.reasons[0] == "STREAM_RECONCILIATION_BLOCKED"
    assert executor.calls == 0
    assert monitor.ack_tokens == []


def test_mainnet_capable_private_stream_monitor_is_hard_rejected() -> None:
    monitor = _Monitor(_Snapshot(healthy=True))
    monitor.live_mainnet_order_routing_allowed = True

    with pytest.raises(ValueError, match="mainnet-capable private stream monitor"):
        run_bybit_product_service(
            config=_config(),
            startup_reconciler=_Reconciler(
                [_startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)]
            ),
            cycle_executor=_Executor(active=False),
            operator_control=_OperatorControl(),
            private_stream_monitor=monitor,
            stop_requested=lambda: False,
            sleep_fn=lambda _: None,
            max_cycles=1,
        )
