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
    ) -> None:
        self.demo_order_writes_enabled = writes
        self.result = _CycleResult() if result is None else result
        self.failure = failure
        self.calls = 0

    def run_once(self) -> object:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result


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


def test_blocked_startup_never_reaches_trading_cycle() -> None:
    reconciler = _Reconciler(_startup(BybitStartupReconciliationStatus.BLOCKED))
    executor = _Executor()

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=reconciler,
        cycle_executor=executor,
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
    executor = _Executor()

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=_Reconciler(_startup(startup_status)),
        cycle_executor=executor,
        stop_requested=lambda: False,
        sleep_fn=lambda _: None,
        max_cycles=1,
    )

    assert result.status is BybitProductServiceStatus.STOPPED
    assert result.completed_cycles == 1
    assert executor.calls == 1


def test_operational_cycle_failure_stops_without_blind_retry() -> None:
    executor = _Executor(failure=TimeoutError("network"))

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=_Reconciler(
            _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)
        ),
        cycle_executor=executor,
        stop_requested=lambda: False,
        sleep_fn=lambda _: None,
        max_cycles=5,
    )

    assert result.status is BybitProductServiceStatus.CYCLE_FAILED
    assert result.reasons == ("TRADING_CYCLE_FAILED:TimeoutError",)
    assert result.completed_cycles == 0
    assert executor.calls == 1


def test_safety_value_error_from_cycle_remains_hard_failure() -> None:
    executor = _Executor(failure=ValueError("unsafe capability"))

    with pytest.raises(ValueError, match="unsafe capability"):
        run_bybit_product_service(
            config=_config(),
            startup_reconciler=_Reconciler(
                _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)
            ),
            cycle_executor=executor,
            stop_requested=lambda: False,
            sleep_fn=lambda _: None,
            max_cycles=1,
        )

    assert executor.calls == 1


def test_mainnet_capable_cycle_result_is_hard_rejected() -> None:
    unsafe = _CycleResult(live_mainnet_order_routing_allowed=True)

    with pytest.raises(ValueError, match="mainnet-capable trading cycle result"):
        run_bybit_product_service(
            config=_config(),
            startup_reconciler=_Reconciler(
                _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)
            ),
            cycle_executor=_Executor(result=unsafe),
            stop_requested=lambda: False,
            sleep_fn=lambda _: None,
            max_cycles=1,
        )


def test_same_invocation_replacement_entry_is_hard_rejected() -> None:
    unsafe = _CycleResult(same_invocation_additional_entry_allowed=True)

    with pytest.raises(ValueError, match="replacement entry"):
        run_bybit_product_service(
            config=_config(),
            startup_reconciler=_Reconciler(
                _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)
            ),
            cycle_executor=_Executor(result=unsafe),
            stop_requested=lambda: False,
            sleep_fn=lambda _: None,
            max_cycles=1,
        )


def test_demo_write_capability_must_exactly_match_config() -> None:
    with pytest.raises(ValueError, match="does not match product config"):
        run_bybit_product_service(
            config=_config(writes=False),
            startup_reconciler=_Reconciler(
                _startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY)
            ),
            cycle_executor=_Executor(writes=True),
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
            stop_requested=lambda: False,
            sleep_fn=lambda _: None,
            max_cycles=1,
        )


def test_stop_request_is_graceful_after_mandatory_startup_reconciliation() -> None:
    reconciler = _Reconciler(_startup(BybitStartupReconciliationStatus.READY_FOR_ENTRY))
    executor = _Executor()

    result = run_bybit_product_service(
        config=_config(),
        startup_reconciler=reconciler,
        cycle_executor=executor,
        stop_requested=lambda: True,
        sleep_fn=lambda _: None,
    )

    assert result.status is BybitProductServiceStatus.STOPPED
    assert result.reasons == ("STOP_REQUESTED",)
    assert result.completed_cycles == 0
    assert reconciler.calls == 1
    assert executor.calls == 0
