from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from typing import Protocol

from app.observability.readiness import (
    OperationalReadiness,
    OperationalReadinessEvaluator,
    OperationalSnapshot,
)


@dataclass(frozen=True)
class BybitRestHealthSnapshot:
    total_calls: int
    window_calls: int
    window_errors: int
    error_fraction: Decimal | None
    last_latency_ms: Decimal | None
    maximum_latency_ms: Decimal | None
    last_success_monotonic: Decimal | None
    last_error_type: str | None


class BybitRestHealthRecorder:
    """Thread-safe operational measurements for existing Bybit REST transports."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, *, window_size: int = 100) -> None:
        if not 1 <= window_size <= 10_000:
            raise ValueError("REST health window_size must be within [1, 10000]")
        self._window: deque[tuple[Decimal, bool]] = deque(maxlen=window_size)
        self._total_calls = 0
        self._last_success_monotonic: Decimal | None = None
        self._last_error_type: str | None = None
        self._lock = Lock()

    def record(
        self,
        *,
        latency_ms: Decimal,
        success: bool,
        observed_monotonic: Decimal,
        error_type: str | None = None,
    ) -> None:
        if not latency_ms.is_finite() or latency_ms < 0:
            raise ValueError("REST latency must be finite and non-negative")
        if not observed_monotonic.is_finite() or observed_monotonic < 0:
            raise ValueError("REST monotonic observation must be finite and non-negative")
        if success and error_type is not None:
            raise ValueError("successful REST observation cannot carry error_type")
        if not success and (error_type is None or not error_type.strip()):
            raise ValueError("failed REST observation requires error_type")
        with self._lock:
            self._total_calls += 1
            self._window.append((latency_ms, not success))
            if success:
                self._last_success_monotonic = observed_monotonic
                self._last_error_type = None
            else:
                self._last_error_type = error_type.strip() if error_type is not None else None

    def snapshot(self) -> BybitRestHealthSnapshot:
        with self._lock:
            samples = tuple(self._window)
            total_calls = self._total_calls
            last_success = self._last_success_monotonic
            last_error = self._last_error_type
        if not samples:
            return BybitRestHealthSnapshot(
                total_calls=total_calls,
                window_calls=0,
                window_errors=0,
                error_fraction=None,
                last_latency_ms=None,
                maximum_latency_ms=None,
                last_success_monotonic=last_success,
                last_error_type=last_error,
            )
        errors = sum(1 for _latency, failed in samples if failed)
        return BybitRestHealthSnapshot(
            total_calls=total_calls,
            window_calls=len(samples),
            window_errors=errors,
            error_fraction=Decimal(errors) / Decimal(len(samples)),
            last_latency_ms=samples[-1][0],
            maximum_latency_ms=max(latency for latency, _failed in samples),
            last_success_monotonic=last_success,
            last_error_type=last_error,
        )


@dataclass(frozen=True)
class BybitMarketDataHealthSnapshot:
    last_success_monotonic: Decimal | None
    market_data_age_seconds: Decimal | None
    market_data_ready: bool


class BybitMarketDataHealthRecorder:
    """Age of the last successful validated market-data observation.

    Flat operation records only after the complete expected bar universe was read successfully.
    Active/pre-entry operation records after an already-validated quote. No extra heartbeat request
    is introduced solely for telemetry.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self) -> None:
        self._last_success_monotonic: Decimal | None = None
        self._lock = Lock()

    def record_success(self, *, observed_monotonic: Decimal) -> None:
        if not observed_monotonic.is_finite() or observed_monotonic < 0:
            raise ValueError("market-data monotonic observation must be finite and non-negative")
        with self._lock:
            self._last_success_monotonic = observed_monotonic

    def snapshot(self, *, now_monotonic: Decimal) -> BybitMarketDataHealthSnapshot:
        if not now_monotonic.is_finite() or now_monotonic < 0:
            raise ValueError("health monotonic clock must be finite and non-negative")
        with self._lock:
            last_success = self._last_success_monotonic
        age = None
        if last_success is not None:
            age = now_monotonic - last_success
            if age < 0:
                raise ValueError("health monotonic clock regressed after market-data observation")
        return BybitMarketDataHealthSnapshot(
            last_success_monotonic=last_success,
            market_data_age_seconds=age,
            market_data_ready=last_success is not None,
        )


class BybitReconciliationResultLike(Protocol):
    status: object
    broker_truth_complete: bool
    live_mainnet_order_routing_allowed: bool


@dataclass(frozen=True)
class BybitReconciliationHealthSnapshot:
    last_success_monotonic: Decimal | None
    reconciliation_age_seconds: Decimal | None
    broker_connected: bool | None
    portfolio_reconciled: bool | None
    position_mismatches: int | None


class BybitReconciliationHealthRecorder:
    """Process-local age/state of actual REST broker-truth reconciliation."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self) -> None:
        self._last_success_monotonic: Decimal | None = None
        self._broker_connected: bool | None = None
        self._portfolio_reconciled: bool | None = None
        self._position_mismatches: int | None = None
        self._lock = Lock()

    def record(
        self,
        result: BybitReconciliationResultLike,
        *,
        observed_monotonic: Decimal,
    ) -> None:
        _reject_live_measurement_source(result, name="reconciliation result")
        if not observed_monotonic.is_finite() or observed_monotonic < 0:
            raise ValueError("reconciliation monotonic observation must be finite and non-negative")
        if not isinstance(result.broker_truth_complete, bool):
            raise ValueError("reconciliation broker-truth completeness must be boolean")
        status = getattr(result.status, "value", result.status)
        if not isinstance(status, str) or not status:
            raise ValueError("reconciliation status is invalid")
        with self._lock:
            if result.broker_truth_complete:
                self._last_success_monotonic = observed_monotonic
                self._broker_connected = True
                reconciled = status in {"READY_FOR_ENTRY", "RESUME_MANAGEMENT"}
                self._portfolio_reconciled = reconciled
                self._position_mismatches = 0 if reconciled else None
            else:
                self._broker_connected = False
                self._portfolio_reconciled = False
                self._position_mismatches = None

    def snapshot(self, *, now_monotonic: Decimal) -> BybitReconciliationHealthSnapshot:
        if not now_monotonic.is_finite() or now_monotonic < 0:
            raise ValueError("health monotonic clock must be finite and non-negative")
        with self._lock:
            last_success = self._last_success_monotonic
            broker_connected = self._broker_connected
            portfolio_reconciled = self._portfolio_reconciled
            position_mismatches = self._position_mismatches
        age = None
        if last_success is not None:
            age = now_monotonic - last_success
            if age < 0:
                raise ValueError("health monotonic clock regressed after reconciliation")
        return BybitReconciliationHealthSnapshot(
            last_success_monotonic=last_success,
            reconciliation_age_seconds=age,
            broker_connected=broker_connected,
            portfolio_reconciled=portfolio_reconciled,
            position_mismatches=position_mismatches,
        )


class BybitPrivateStreamHealthLike(Protocol):
    healthy: bool
    last_message_monotonic: float | None
    live_mainnet_order_routing_allowed: bool


class BybitOperatorHealthLike(Protocol):
    kill_switch_engaged: bool
    live_mainnet_order_routing_allowed: bool


@dataclass(frozen=True)
class BybitOperationalMeasurements:
    market_data_age_seconds: Decimal | None = None
    stream_silence_seconds: Decimal | None = None
    broker_latency_ms: Decimal | None = None
    broker_error_fraction: Decimal | None = None
    uncertain_orders: int | None = None
    reconciliation_age_seconds: Decimal | None = None
    cash_mismatch: Decimal | None = None
    position_mismatches: int | None = None
    daily_pnl: Decimal | None = None
    drawdown: Decimal | None = None
    kill_switch_engaged: bool | None = None
    market_data_ready: bool | None = None
    stream_ready: bool | None = None
    broker_connected: bool | None = None
    portfolio_reconciled: bool | None = None


@dataclass(frozen=True)
class BybitOperationalHealthReport:
    measurement_complete: bool
    blockers: tuple[str, ...]
    snapshot: OperationalSnapshot | None
    readiness: OperationalReadiness | None
    live_mainnet_order_routing_allowed: bool = False


def collect_bybit_operational_measurements(
    *,
    now_monotonic: Decimal,
    market_data: BybitMarketDataHealthSnapshot,
    rest: BybitRestHealthSnapshot,
    reconciliation: BybitReconciliationHealthSnapshot,
    private_stream: BybitPrivateStreamHealthLike | None,
    unresolved_entry_submissions: int | None,
    operator: BybitOperatorHealthLike | None,
) -> BybitOperationalMeasurements:
    """Collect only measurements proven by existing authoritative runtime sources."""

    if not now_monotonic.is_finite() or now_monotonic < 0:
        raise ValueError("health monotonic clock must be finite and non-negative")
    if unresolved_entry_submissions is not None:
        if isinstance(unresolved_entry_submissions, bool) or unresolved_entry_submissions < 0:
            raise ValueError("unresolved entry submission count must be non-negative")

    stream_silence: Decimal | None = None
    stream_ready: bool | None = None
    if private_stream is not None:
        _reject_live_measurement_source(private_stream, name="private stream snapshot")
        if not isinstance(private_stream.healthy, bool):
            raise ValueError("private stream health must be boolean")
        stream_ready = private_stream.healthy
        if private_stream.last_message_monotonic is not None:
            last_message = Decimal(str(private_stream.last_message_monotonic))
            if not last_message.is_finite() or last_message < 0:
                raise ValueError("private stream last-message monotonic value is invalid")
            stream_silence = now_monotonic - last_message
            if stream_silence < 0:
                raise ValueError("health monotonic clock regressed after private stream message")

    kill_switch: bool | None = None
    if operator is not None:
        _reject_live_measurement_source(operator, name="operator snapshot")
        if not isinstance(operator.kill_switch_engaged, bool):
            raise ValueError("operator kill-switch state must be boolean")
        kill_switch = operator.kill_switch_engaged

    return BybitOperationalMeasurements(
        market_data_age_seconds=market_data.market_data_age_seconds,
        stream_silence_seconds=stream_silence,
        broker_latency_ms=rest.maximum_latency_ms,
        broker_error_fraction=rest.error_fraction,
        uncertain_orders=unresolved_entry_submissions,
        reconciliation_age_seconds=reconciliation.reconciliation_age_seconds,
        position_mismatches=reconciliation.position_mismatches,
        kill_switch_engaged=kill_switch,
        market_data_ready=market_data.market_data_ready,
        stream_ready=stream_ready,
        broker_connected=reconciliation.broker_connected,
        portfolio_reconciled=reconciliation.portfolio_reconciled,
    )


def build_bybit_operational_health(
    measurements: BybitOperationalMeasurements,
    *,
    evaluator: OperationalReadinessEvaluator | None = None,
) -> BybitOperationalHealthReport:
    fields = (
        "market_data_age_seconds",
        "stream_silence_seconds",
        "broker_latency_ms",
        "broker_error_fraction",
        "uncertain_orders",
        "reconciliation_age_seconds",
        "cash_mismatch",
        "position_mismatches",
        "daily_pnl",
        "drawdown",
        "kill_switch_engaged",
        "market_data_ready",
        "stream_ready",
        "broker_connected",
        "portfolio_reconciled",
    )
    missing = tuple(
        f"MEASUREMENT_UNAVAILABLE:{name}"
        for name in fields
        if getattr(measurements, name) is None
    )
    if missing:
        return BybitOperationalHealthReport(
            measurement_complete=False,
            blockers=missing,
            snapshot=None,
            readiness=None,
        )

    snapshot = OperationalSnapshot(
        market_data_age_seconds=_decimal(measurements.market_data_age_seconds),
        stream_silence_seconds=_decimal(measurements.stream_silence_seconds),
        broker_latency_ms=_decimal(measurements.broker_latency_ms),
        broker_error_fraction=_decimal(measurements.broker_error_fraction),
        uncertain_orders=_integer(measurements.uncertain_orders),
        reconciliation_age_seconds=_decimal(measurements.reconciliation_age_seconds),
        cash_mismatch=_decimal(measurements.cash_mismatch),
        position_mismatches=_integer(measurements.position_mismatches),
        daily_pnl=_decimal(measurements.daily_pnl),
        drawdown=_decimal(measurements.drawdown),
        kill_switch_engaged=_boolean(measurements.kill_switch_engaged),
        market_data_ready=_boolean(measurements.market_data_ready),
        stream_ready=_boolean(measurements.stream_ready),
        broker_connected=_boolean(measurements.broker_connected),
        portfolio_reconciled=_boolean(measurements.portfolio_reconciled),
        external_order_routing_allowed=False,
        live_trading_allowed=False,
    )
    active = OperationalReadinessEvaluator() if evaluator is None else evaluator
    readiness = active.evaluate(snapshot)
    return BybitOperationalHealthReport(
        measurement_complete=True,
        blockers=readiness.reasons,
        snapshot=snapshot,
        readiness=readiness,
    )


def _reject_live_measurement_source(value: object, *, name: str) -> None:
    if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"Bybit health rejected mainnet-capable {name}")


def _decimal(value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError("completed operational decimal measurement must be Decimal")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("completed operational count measurement must be int")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("completed operational boolean measurement must be bool")
    return value
