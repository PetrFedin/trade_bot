from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, TypeVar

from app.execution.bybit_demo import BybitDemoHttpJson
from app.execution.bybit_demo_account_reader import BybitDemoAccountingClient
from app.execution.bybit_demo_broker_truth import BybitDemoBrokerTruthClient
from app.execution.bybit_demo_stop_ratchet_client import BybitDemoStopRatchetClient

T = TypeVar("T")
MonotonicFn = Callable[[], float]


class BybitRestHealthSink(Protocol):
    def record(
        self,
        *,
        latency_ms: Decimal,
        success: bool,
        observed_monotonic: Decimal,
        error_type: str | None = None,
    ) -> None: ...


class _BybitRestHealthMixin:
    """Non-authoritative measurement side channel for existing REST clients.

    Telemetry failures are exposed through ``rest_health_recording_error_type`` but never replace
    a broker result or broker exception. Trading/reconciliation semantics remain owned by the
    underlying client.
    """

    _rest_health_sink: BybitRestHealthSink
    _rest_health_monotonic: MonotonicFn
    _rest_health_recording_error_type: str | None

    def _configure_rest_health(
        self,
        *,
        rest_health_sink: BybitRestHealthSink,
        monotonic_fn: MonotonicFn,
    ) -> None:
        self._rest_health_sink = rest_health_sink
        self._rest_health_monotonic = monotonic_fn
        self._rest_health_recording_error_type = None

    @property
    def rest_health_recording_error_type(self) -> str | None:
        return self._rest_health_recording_error_type

    def _observe_rest_call(self, operation: Callable[[], T]) -> T:
        started = self._safe_monotonic()
        try:
            result = operation()
        except Exception as exc:
            finished = self._safe_monotonic()
            if started is not None and finished is not None:
                self._record_rest_health(
                    latency_ms=(finished - started) * Decimal("1000"),
                    success=False,
                    observed_monotonic=finished,
                    error_type=type(exc).__name__,
                )
            raise
        finished = self._safe_monotonic()
        if started is not None and finished is not None:
            self._record_rest_health(
                latency_ms=(finished - started) * Decimal("1000"),
                success=True,
                observed_monotonic=finished,
            )
        return result

    def _safe_monotonic(self) -> Decimal | None:
        try:
            raw = self._rest_health_monotonic()
            if isinstance(raw, bool):
                raise ValueError("REST telemetry monotonic clock returned boolean")
            value = Decimal(str(raw))
            if not value.is_finite() or value < 0:
                raise ValueError("REST telemetry monotonic clock must be finite and non-negative")
            return value
        except (InvalidOperation, TypeError, ValueError) as exc:
            self._rest_health_recording_error_type = type(exc).__name__
            return None

    def _record_rest_health(
        self,
        *,
        latency_ms: Decimal,
        success: bool,
        observed_monotonic: Decimal,
        error_type: str | None = None,
    ) -> None:
        try:
            self._rest_health_sink.record(
                latency_ms=latency_ms,
                success=success,
                observed_monotonic=observed_monotonic,
                error_type=error_type,
            )
        except Exception as exc:
            self._rest_health_recording_error_type = type(exc).__name__


class _ObservedBybitOrderClientMixin(_BybitRestHealthMixin):
    def _signed_get(self, path: str, params: Mapping[str, str]) -> BybitDemoHttpJson:
        operation = super()._signed_get  # type: ignore[misc]
        return self._observe_rest_call(lambda: operation(path, params))

    def _signed_post(self, path: str, payload: Mapping[str, Any]) -> BybitDemoHttpJson:
        operation = super()._signed_post  # type: ignore[misc]
        return self._observe_rest_call(lambda: operation(path, payload))


class ObservedBybitDemoStopRatchetClient(
    _ObservedBybitOrderClientMixin,
    BybitDemoStopRatchetClient,
):
    def __init__(
        self,
        *,
        rest_health_sink: BybitRestHealthSink,
        monotonic_fn: MonotonicFn = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._configure_rest_health(
            rest_health_sink=rest_health_sink,
            monotonic_fn=monotonic_fn,
        )


class ObservedBybitDemoBrokerTruthClient(
    _ObservedBybitOrderClientMixin,
    BybitDemoBrokerTruthClient,
):
    def __init__(
        self,
        *,
        rest_health_sink: BybitRestHealthSink,
        monotonic_fn: MonotonicFn = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._configure_rest_health(
            rest_health_sink=rest_health_sink,
            monotonic_fn=monotonic_fn,
        )


class ObservedBybitDemoAccountingClient(
    _BybitRestHealthMixin,
    BybitDemoAccountingClient,
):
    def __init__(
        self,
        *,
        rest_health_sink: BybitRestHealthSink,
        monotonic_fn: MonotonicFn = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._configure_rest_health(
            rest_health_sink=rest_health_sink,
            monotonic_fn=monotonic_fn,
        )

    def _private_get_result(
        self,
        *,
        path: str,
        query: Mapping[str, str],
    ) -> Mapping[str, Any]:
        operation = super()._private_get_result
        return self._observe_rest_call(lambda: operation(path=path, query=query))
