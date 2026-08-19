from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.execution.bybit_startup_reconciliation import (
    BybitStartupReconciliationResult,
    BybitStartupReconciliationStatus,
)
from app.runtime.bybit_product_config import BybitProductConfig


class BybitProductServiceStatus(StrEnum):
    STOPPED = "STOPPED"
    STARTUP_BLOCKED = "STARTUP_BLOCKED"
    STARTUP_FAILED = "STARTUP_FAILED"
    CYCLE_FAILED = "CYCLE_FAILED"


@dataclass(frozen=True)
class BybitProductServiceResult:
    status: BybitProductServiceStatus
    reasons: tuple[str, ...]
    completed_cycles: int
    startup: BybitStartupReconciliationResult | None
    last_cycle_result: object | None
    graceful_stop: bool
    diagnostics_only: bool = True
    strategy_retuning_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


class BybitStartupReconciler(Protocol):
    live_mainnet_order_routing_allowed: bool

    def run(self) -> BybitStartupReconciliationResult: ...


class BybitCycleExecutor(Protocol):
    live_mainnet_order_routing_allowed: bool
    demo_order_writes_enabled: bool

    def run_once(self) -> object: ...


StopRequested = Callable[[], bool]
SleepFn = Callable[[float], None]


def run_bybit_product_service(
    *,
    config: BybitProductConfig,
    startup_reconciler: BybitStartupReconciler,
    cycle_executor: BybitCycleExecutor,
    stop_requested: StopRequested,
    sleep_fn: SleepFn = time.sleep,
    max_cycles: int | None = None,
) -> BybitProductServiceResult:
    """Run the canonical fail-closed Bybit product supervisor.

    Startup broker truth is mandatory before the first cycle. A blocked startup never reaches the
    trading runtime. Management/terminal-recovery states may run because the existing single-writer
    trading router treats the durable checkpoint as authoritative and forbids replacement entry in
    the same invocation. Operational runtime exceptions stop the service; safety/capability
    ``ValueError`` exceptions remain hard failures and are never downgraded to diagnostics.
    """

    config.validate()
    _reject_live_capability(startup_reconciler, name="startup reconciler")
    _validate_cycle_executor(config, cycle_executor)
    if max_cycles is not None and max_cycles < 0:
        raise ValueError("max_cycles must be non-negative or None")

    try:
        startup = startup_reconciler.run()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - operational startup failure stops the service.
        return _result(
            BybitProductServiceStatus.STARTUP_FAILED,
            reasons=(f"STARTUP_RECONCILIATION_FAILED:{type(exc).__name__}",),
            completed_cycles=0,
            startup=None,
            last_cycle_result=None,
            graceful_stop=False,
        )

    _reject_live_capability(startup, name="startup reconciliation result")
    if not startup.broker_truth_complete:
        return _result(
            BybitProductServiceStatus.STARTUP_BLOCKED,
            reasons=("STARTUP_BROKER_TRUTH_INCOMPLETE",) + startup.reasons,
            completed_cycles=0,
            startup=startup,
            last_cycle_result=None,
            graceful_stop=False,
        )
    if startup.status is BybitStartupReconciliationStatus.BLOCKED:
        return _result(
            BybitProductServiceStatus.STARTUP_BLOCKED,
            reasons=startup.reasons,
            completed_cycles=0,
            startup=startup,
            last_cycle_result=None,
            graceful_stop=False,
        )
    if startup.status not in {
        BybitStartupReconciliationStatus.READY_FOR_ENTRY,
        BybitStartupReconciliationStatus.RESUME_MANAGEMENT,
        BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED,
    }:
        raise ValueError("Bybit product service rejected unknown startup status")

    completed = 0
    last_cycle_result: object | None = None
    while True:
        if stop_requested():
            return _result(
                BybitProductServiceStatus.STOPPED,
                reasons=("STOP_REQUESTED",),
                completed_cycles=completed,
                startup=startup,
                last_cycle_result=last_cycle_result,
                graceful_stop=True,
            )
        if max_cycles is not None and completed >= max_cycles:
            return _result(
                BybitProductServiceStatus.STOPPED,
                reasons=("MAX_CYCLES_REACHED",),
                completed_cycles=completed,
                startup=startup,
                last_cycle_result=last_cycle_result,
                graceful_stop=True,
            )

        try:
            cycle_result = cycle_executor.run_once()
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - do not retry a possibly ambiguous mutation.
            return _result(
                BybitProductServiceStatus.CYCLE_FAILED,
                reasons=(f"TRADING_CYCLE_FAILED:{type(exc).__name__}",),
                completed_cycles=completed,
                startup=startup,
                last_cycle_result=last_cycle_result,
                graceful_stop=False,
            )

        _reject_live_capability(cycle_result, name="trading cycle result")
        _validate_cycle_result(cycle_result)
        completed += 1
        last_cycle_result = cycle_result

        if stop_requested():
            continue
        if max_cycles is not None and completed >= max_cycles:
            continue
        sleep_fn(config.poll_interval_ms / 1000)


def _validate_cycle_executor(
    config: BybitProductConfig,
    executor: BybitCycleExecutor,
) -> None:
    _reject_live_capability(executor, name="cycle executor")
    if not isinstance(executor.demo_order_writes_enabled, bool):
        raise ValueError("cycle executor demo write capability must be boolean")
    if executor.demo_order_writes_enabled != config.demo_order_writes_allowed:
        raise ValueError("cycle executor demo write capability does not match product config")


def _validate_cycle_result(result: object) -> None:
    if getattr(result, "same_invocation_additional_entry_allowed", False) is not False:
        raise ValueError("cycle result permitted same-invocation replacement entry")


def _reject_live_capability(value: object, *, name: str) -> None:
    if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"Bybit product service rejected mainnet-capable {name}")


def _result(
    status: BybitProductServiceStatus,
    *,
    reasons: tuple[str, ...],
    completed_cycles: int,
    startup: BybitStartupReconciliationResult | None,
    last_cycle_result: object | None,
    graceful_stop: bool,
) -> BybitProductServiceResult:
    return BybitProductServiceResult(
        status=status,
        reasons=reasons,
        completed_cycles=completed_cycles,
        startup=startup,
        last_cycle_result=last_cycle_result,
        graceful_stop=graceful_stop,
    )
