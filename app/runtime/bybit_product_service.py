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

    def has_active_trade(self) -> bool: ...


class BybitPrivateStreamSnapshotLike(Protocol):
    healthy: bool
    reconciliation_required: bool
    reconciliation_token: int
    live_mainnet_order_routing_allowed: bool


class BybitPrivateStreamMonitor(Protocol):
    live_mainnet_order_routing_allowed: bool

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def snapshot(self) -> BybitPrivateStreamSnapshotLike: ...

    def acknowledge_reconciliation(self, *, token: int) -> bool: ...

    def wait(self, timeout_seconds: float) -> None: ...


StopRequested = Callable[[], bool]
SleepFn = Callable[[float], None]


def run_bybit_product_service(
    *,
    config: BybitProductConfig,
    startup_reconciler: BybitStartupReconciler,
    cycle_executor: BybitCycleExecutor,
    stop_requested: StopRequested,
    private_stream_monitor: BybitPrivateStreamMonitor | None = None,
    sleep_fn: SleepFn = time.sleep,
    max_cycles: int | None = None,
) -> BybitProductServiceResult:
    """Run the canonical fail-closed Bybit product supervisor.

    REST remains broker truth. The private stream is only a reaction/health layer: reconnects and
    trade events force a fresh REST reconciliation before the next decision. If the stream is
    unhealthy while flat, new entries pause. An already-active trade continues REST management so
    loss protection is not abandoned merely because the notification channel is unavailable.
    """

    config.validate()
    _reject_live_capability(startup_reconciler, name="startup reconciler")
    _validate_cycle_executor(config, cycle_executor)
    if private_stream_monitor is not None:
        _reject_live_capability(private_stream_monitor, name="private stream monitor")
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
    blocked = _startup_block_reason(startup)
    if blocked is not None:
        return _result(
            BybitProductServiceStatus.STARTUP_BLOCKED,
            reasons=blocked,
            completed_cycles=0,
            startup=startup,
            last_cycle_result=None,
            graceful_stop=False,
        )
    _validate_runnable_startup_status(startup.status)

    if private_stream_monitor is not None:
        try:
            private_stream_monitor.start()
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - process-level stream startup cannot be trusted.
            return _result(
                BybitProductServiceStatus.STARTUP_FAILED,
                reasons=(f"PRIVATE_STREAM_START_FAILED:{type(exc).__name__}",),
                completed_cycles=0,
                startup=startup,
                last_cycle_result=None,
                graceful_stop=False,
            )

    completed = 0
    last_cycle_result: object | None = None
    try:
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

            stream_snapshot = None
            if private_stream_monitor is not None:
                stream_snapshot = private_stream_monitor.snapshot()
                _reject_live_capability(stream_snapshot, name="private stream snapshot")
                if stream_snapshot.reconciliation_required:
                    reconciliation = _reconcile_after_stream_event(startup_reconciler)
                    if isinstance(reconciliation, BybitProductServiceResult):
                        return _result(
                            reconciliation.status,
                            reasons=reconciliation.reasons,
                            completed_cycles=completed,
                            startup=startup,
                            last_cycle_result=last_cycle_result,
                            graceful_stop=False,
                        )
                    startup = reconciliation
                    if not private_stream_monitor.acknowledge_reconciliation(
                        token=stream_snapshot.reconciliation_token
                    ):
                        continue
                    stream_snapshot = private_stream_monitor.snapshot()
                    _reject_live_capability(stream_snapshot, name="private stream snapshot")

                if not stream_snapshot.healthy:
                    try:
                        active_trade = _active_trade_state(cycle_executor)
                    except ValueError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - unknown local state blocks entry.
                        return _result(
                            BybitProductServiceStatus.CYCLE_FAILED,
                            reasons=(
                                f"ACTIVE_TRADE_STATE_READ_FAILED:{type(exc).__name__}",
                            ),
                            completed_cycles=completed,
                            startup=startup,
                            last_cycle_result=last_cycle_result,
                            graceful_stop=False,
                        )
                    if not active_trade:
                        private_stream_monitor.wait(config.poll_interval_ms / 1000)
                        continue

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
            if private_stream_monitor is None:
                sleep_fn(config.poll_interval_ms / 1000)
            else:
                private_stream_monitor.wait(config.poll_interval_ms / 1000)
    finally:
        if private_stream_monitor is not None:
            private_stream_monitor.stop()


def _reconcile_after_stream_event(
    startup_reconciler: BybitStartupReconciler,
) -> BybitStartupReconciliationResult | BybitProductServiceResult:
    try:
        reconciliation = startup_reconciler.run()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - incomplete REST truth blocks the next decision.
        return _result(
            BybitProductServiceStatus.CYCLE_FAILED,
            reasons=(f"STREAM_RECONCILIATION_FAILED:{type(exc).__name__}",),
            completed_cycles=0,
            startup=None,
            last_cycle_result=None,
            graceful_stop=False,
        )
    _reject_live_capability(reconciliation, name="stream reconciliation result")
    blocked = _startup_block_reason(reconciliation)
    if blocked is not None:
        return _result(
            BybitProductServiceStatus.CYCLE_FAILED,
            reasons=("STREAM_RECONCILIATION_BLOCKED",) + blocked,
            completed_cycles=0,
            startup=reconciliation,
            last_cycle_result=None,
            graceful_stop=False,
        )
    _validate_runnable_startup_status(reconciliation.status)
    return reconciliation


def _startup_block_reason(
    startup: BybitStartupReconciliationResult,
) -> tuple[str, ...] | None:
    if not startup.broker_truth_complete:
        return ("STARTUP_BROKER_TRUTH_INCOMPLETE",) + startup.reasons
    if startup.status is BybitStartupReconciliationStatus.BLOCKED:
        return startup.reasons
    return None


def _validate_runnable_startup_status(status: BybitStartupReconciliationStatus) -> None:
    if status not in {
        BybitStartupReconciliationStatus.READY_FOR_ENTRY,
        BybitStartupReconciliationStatus.RESUME_MANAGEMENT,
        BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED,
    }:
        raise ValueError("Bybit product service rejected unknown startup status")


def _active_trade_state(executor: BybitCycleExecutor) -> bool:
    active = executor.has_active_trade()
    if not isinstance(active, bool):
        raise ValueError("cycle executor active-trade state must be boolean")
    return active


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
