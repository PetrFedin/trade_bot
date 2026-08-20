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


@dataclass
class _CycleResult:
    live_mainnet_order_routing_allowed: bool = False
    same_invocation_additional_entry_allowed: bool = False


@dataclass(frozen=True)
class _OperatorSnapshot:
    new_entries_allowed: bool = True
    active_trade_safety_management_allowed: bool = True
    live_mainnet_order_routing_allowed: bool = False


class _OperatorControl:
    live_mainnet_order_routing_allowed = False
    active_trade_safety_management_allowed = True

    def __init__(self, *responses: object) -> None:
        self.responses = responses or (_OperatorSnapshot(),)
        self.calls = 0

    def inspect(self) -> _OperatorSnapshot:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, _OperatorSnapshot)
        return response


class _Reconciler:
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        result: BybitStartupReconciliationResult,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.calls = 0

    def run(self) -> BybitStartupReconciliationResult:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result


class _Executor:
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        *,
        writes: bool = False,
        result: object | None = None,
        failure: Exception | None = None,
        active_trade: bool = False,
    ) -> None:
        self.demo_order_writes_enabled = writes
        self.result = _CycleResult() if result is None else result
        self.failure = failure
        self.active_trade = active_trade
        self.calls = 0
        self.active_trade_reads = 0

    def run_once(self) -> object:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result

    def has_active_trade(self) -> bool:
        self.active_trade_reads += 1
        return self.active_trade


def _config(*, writes: bool = False) -> BybitProductConfig:
    return BybitProductConfig.from_env(
        {
            "ASTRA_ENV": "demo",
            "ASTRA_BROKER": "bybit",
            "BYBIT_API_KEY": "demo-key",
            "BYBIT_API_SECRET": "demo-secret",
            "DATABASE_URL": "postgresql://astra:secret@db.example/astra",
            "TRADING_WRITES_ENABLED": "true" if writes else "false",
            "MAINNET_ENABLED": "false",
        }
    )


def _startup(
    status: BybitStartupReconciliationStatus,
    *,
    broker_truth_complete: bool = True,
) -> BybitStartupReconciliationResult:
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
        broker_truth_complete=broker_truth_complete,
    )


def _run(
    *,
    startup: BybitStartupReconciliationStatus = BybitStartupReconciliationStatus.READY_FOR_ENTRY,
    executor: _Executor | None = None,
    operator: _OperatorControl | None = None,
    writes: bool = False,
    sleep_fn=lambda _delay: None,
    max_cycles: int | None = 1,
):
    return run_bybit_product_service(
        config=_config(writes=writes),
        startup_reconciler=_Reconciler(_startup(startup)),
        cycle_executor=_Executor(writes=writes) if executor is None else executor,
        operator_control=_OperatorControl() if operator is None else operator,
        stop_requested=lambda: False,
        sleep_fn=sleep_fn,
        max_cycles=max_cycles,
    )


def test_blocked_startup_never_reaches_trading_cycle() -> None:
    reconciler = _Reconciler(_startup(BybitStartupReconciliationStatus.BLOCKED))
    executor = _Executor()

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=reconciler,
        cycle_executor=executor,
        operator_control=_OperatorControl(),
        stop_requested=lambda: False,
        sleep_fn=lambda _: None,
        max_cycles=1,
    )

    assert result.status is BybitProductServiceStatus.STARTUP_BLOCKED
    assert result.completed_cycles == 0
    assert executor.calls == 0


def test_ready_startup_runs_bounded_cycles_and_stops_gracefully() -> None:
    executor = _Executor()
    sleeps: list[float] = []

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=_Reconciler(
            _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)
        ),
        cycle_executor=executor,
        operator_control=_OperatorControl(),
        stop_requested=lambda: False,
        sleep_fn=sleeps.append,
        max_cycles=2,
    )

    assert result.status is BybitProductServiceStatus.STOPPED
    assert result.reasons == ("MAX_CYCLES_REACHED",)
    assert result.completed_cycles == 2
    assert result.graceful_stop is True
    assert executor.calls == 2
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    "startup_status",
    [
        BybitStartupReconciliationStatus.RESUME_MANAGEMENT,
        BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED,
    ],
)
def test_management_and_terminal_recovery_are_allowed_to_run_existing_router(
    startup_status: BybitStartupReconciliationStatus,
) -> None:
    executor = _Executor(active_trade=True)

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=_Reconciler(_startup(startup_status)),
        cycle_executor=executor,
        operator_control=_OperatorControl(_OperatorSnapshot(new_entries_allowed=False)),
        stop_requested=lambda: False,
        sleep_fn=lambda _: None,
        max_cycles=1,
    )

    assert result.status is BybitProductServiceStatus.STOPPED
    assert result.completed_cycles == 1
    assert executor.calls == 1


def test_paused_flat_runtime_waits_until_operator_resumes_new_entries() -> None:
    executor = _Executor(active_trade=False)
    sleeps: list[float] = []
    paused = _OperatorSnapshot(new_entries_allowed=False)
    operator = _OperatorControl(paused, paused, _OperatorSnapshot())

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=_Reconciler(
            _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)
        ),
        cycle_executor=executor,
        operator_control=operator,
        stop_requested=lambda: False,
        sleep_fn=sleeps.append,
        max_cycles=1,
    )

    assert result.status is BybitProductServiceStatus.STOPPED
    assert result.completed_cycles == 1
    assert executor.calls == 1
    assert executor.active_trade_reads == 1
    assert sleeps == [1.0]
    assert operator.calls == 3


def test_killed_or_read_only_state_does_not_abandon_active_trade_management() -> None:
    executor = _Executor(active_trade=True)
    blocked = _OperatorSnapshot(new_entries_allowed=False)

    result = _run(executor=executor, operator=_OperatorControl(blocked))

    assert result.status is BybitProductServiceStatus.STOPPED
    assert result.completed_cycles == 1
    assert executor.calls == 1
    assert executor.active_trade_reads == 1


def test_transient_operator_read_failure_blocks_flat_entry_until_state_recovers() -> None:
    executor = _Executor(active_trade=False)
    sleeps: list[float] = []
    operator = _OperatorControl(
        _OperatorSnapshot(),
        TimeoutError("operator db unavailable"),
        _OperatorSnapshot(),
    )

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=_Reconciler(
            _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)
        ),
        cycle_executor=executor,
        operator_control=operator,
        stop_requested=lambda: False,
        sleep_fn=sleeps.append,
        max_cycles=1,
    )

    assert result.status is BybitProductServiceStatus.STOPPED
    assert result.completed_cycles == 1
    assert executor.calls == 1
    assert executor.active_trade_reads == 1
    assert sleeps == [1.0]


def test_transient_operator_read_failure_keeps_active_trade_management_running() -> None:
    executor = _Executor(active_trade=True)
    operator = _OperatorControl(
        _OperatorSnapshot(new_entries_allowed=False),
        TimeoutError("operator db unavailable"),
    )

    result = _run(executor=executor, operator=operator)

    assert result.status is BybitProductServiceStatus.STOPPED
    assert result.completed_cycles == 1
    assert executor.calls == 1
    assert executor.active_trade_reads == 1


def test_flat_startup_requires_readable_operator_authority() -> None:
    executor = _Executor()
    operator = _OperatorControl(TimeoutError("operator db unavailable"))

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=_Reconciler(
            _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)
        ),
        cycle_executor=executor,
        operator_control=operator,
        stop_requested=lambda: False,
        sleep_fn=lambda _: None,
        max_cycles=1,
    )

    assert result.status is BybitProductServiceStatus.STARTUP_FAILED
    assert result.reasons == ("OPERATOR_CONTROL_READ_FAILED:TimeoutError",)
    assert result.completed_cycles == 0
    assert executor.calls == 0


def test_operational_cycle_failure_stops_without_blind_retry() -> None:
    executor = _Executor(failure=TimeoutError("network"))

    result = _run(executor=executor, max_cycles=5)

    assert result.status is BybitProductServiceStatus.CYCLE_FAILED
    assert result.reasons == ("TRADING_CYCLE_FAILED:TimeoutError",)
    assert result.completed_cycles == 0
    assert executor.calls == 1


def test_safety_value_error_from_cycle_remains_hard_failure() -> None:
    executor = _Executor(failure=ValueError("unsafe capability"))

    with pytest.raises(ValueError, match="unsafe capability"):
        _run(executor=executor)

    assert executor.calls == 1


def test_mainnet_capable_cycle_result_is_hard_rejected() -> None:
    unsafe = _CycleResult(live_mainnet_order_routing_allowed=True)

    with pytest.raises(ValueError, match="mainnet-capable trading cycle result"):
        _run(executor=_Executor(result=unsafe))


def test_mainnet_capable_operator_control_is_hard_rejected() -> None:
    operator = _OperatorControl()
    operator.live_mainnet_order_routing_allowed = True  # type: ignore[misc]

    with pytest.raises(ValueError, match="mainnet-capable operator control"):
        _run(operator=operator)


def test_operator_snapshot_cannot_disable_active_trade_safety_management() -> None:
    unsafe = _OperatorSnapshot(active_trade_safety_management_allowed=False)

    with pytest.raises(ValueError, match="disabled active-trade safety management"):
        _run(operator=_OperatorControl(unsafe))


def test_same_invocation_replacement_entry_is_hard_rejected() -> None:
    unsafe = _CycleResult(same_invocation_additional_entry_allowed=True)

    with pytest.raises(ValueError, match="replacement entry"):
        _run(executor=_Executor(result=unsafe))


def test_demo_write_capability_must_exactly_match_config() -> None:
    with pytest.raises(ValueError, match="does not match product config"):
        run_bybit_product_service(
            config=_config(writes=False),
            startup_reconciler=_Reconciler(
                _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)
            ),
            cycle_executor=_Executor(writes=True),
            operator_control=_OperatorControl(),
            stop_requested=lambda: False,
            sleep_fn=lambda _: None,
            max_cycles=1,
        )


def test_operational_startup_failure_stops_before_cycle() -> None:
    executor = _Executor()
    reconciler = _Reconciler(
        _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY),
        failure=TimeoutError("broker unavailable"),
    )

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=reconciler,
        cycle_executor=executor,
        operator_control=_OperatorControl(),
        stop_requested=lambda: False,
        sleep_fn=lambda _: None,
        max_cycles=1,
    )

    assert result.status is BybitProductServiceStatus.STARTUP_FAILED
    assert result.reasons == ("STARTUP_RECONCILIATION_FAILED:TimeoutError",)
    assert executor.calls == 0


def test_safety_value_error_from_startup_remains_hard_failure() -> None:
    reconciler = _Reconciler(
        _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY),
        failure=ValueError("unsafe startup dependency"),
    )

    with pytest.raises(ValueError, match="unsafe startup dependency"):
        run_bybit_product_service(
            config=_config(),
            startup_reconciler=reconciler,
            cycle_executor=_Executor(),
            operator_control=_OperatorControl(),
            stop_requested=lambda: False,
            sleep_fn=lambda _: None,
            max_cycles=1,
        )


def test_stop_request_is_graceful_after_mandatory_startup_reconciliation() -> None:
    reconciler = _Reconciler(_startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY))
    executor = _Executor()
    operator = _OperatorControl()

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=reconciler,
        cycle_executor=executor,
        operator_control=operator,
        stop_requested=lambda: True,
        sleep_fn=lambda _: None,
    )

    assert result.status is BybitProductServiceStatus.STOPPED
    assert result.reasons == ("STOP_REQUESTED",)
    assert result.completed_cycles == 0
    assert reconciler.calls == 1
    assert operator.calls == 1
    assert executor.calls == 0
