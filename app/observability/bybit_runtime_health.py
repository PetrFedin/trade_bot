from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock

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
    """Thread-safe operational measurements for existing Bybit REST transports.

    This is telemetry only. It never authorizes routing and intentionally stores no URLs,
    credentials, request bodies or response payloads.
    """

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


def build_bybit_operational_health(
    measurements: BybitOperationalMeasurements,
    *,
    evaluator: OperationalReadinessEvaluator | None = None,
) -> BybitOperationalHealthReport:
    """Build canonical readiness only from fully measured inputs.

    Missing telemetry is a blocker, never a healthy zero. This prevents a partially instrumented
    runtime from emitting a misleading green operational state during the production transition.
    """

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
