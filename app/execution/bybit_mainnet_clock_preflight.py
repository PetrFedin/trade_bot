from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.execution.bybit_mainnet_readonly import (
    BybitMainnetReadOnlyHttpJson,
    validate_bybit_mainnet_readonly_host,
)
from app.execution.bybit_rest_policy import (
    BybitRestPolicy,
    BybitRestProtocolError,
    SleepFn,
    raise_for_bybit_response,
    run_bybit_read_with_retry,
)

_SERVER_TIME_PATH = "/v5/market/time"
_MAX_SAFE_CLOCK_UNCERTAINTY_MS = 500
_MAX_SAFE_ROUND_TRIP_MS = 1000


class BybitMainnetClockPreflightError(RuntimeError):
    """Raised when Bybit server-time readiness cannot be established safely."""


class BybitMainnetServerTimeTransport(Protocol):
    def get_server_time(self) -> Mapping[str, Any] | BybitMainnetReadOnlyHttpJson: ...


@dataclass(frozen=True)
class BybitMainnetClockPreflight:
    api_host: str
    local_send_time_ms: int
    local_receive_time_ms: int
    server_time_ms: int
    round_trip_time_ms: int
    estimated_clock_offset_ms: int
    uncertainty_ms: int
    worst_case_abs_clock_skew_ms: int
    max_safe_clock_uncertainty_ms: int = _MAX_SAFE_CLOCK_UNCERTAINTY_MS
    max_safe_round_trip_ms: int = _MAX_SAFE_ROUND_TRIP_MS
    environment: str = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed: bool = False
    order_writes_supported: bool = False

    @property
    def ready(self) -> bool:
        return (
            self.worst_case_abs_clock_skew_ms <= self.max_safe_clock_uncertainty_ms
            and self.round_trip_time_ms <= self.max_safe_round_trip_ms
            and self.environment == "BYBIT_MAINNET_READONLY"
            and not self.live_mainnet_order_routing_allowed
            and not self.order_writes_supported
        )

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.worst_case_abs_clock_skew_ms > self.max_safe_clock_uncertainty_ms:
            reasons.append("BYBIT_MAINNET_CLOCK_SKEW_UNSAFE")
        if self.round_trip_time_ms > self.max_safe_round_trip_ms:
            reasons.append("BYBIT_MAINNET_CLOCK_RTT_UNSAFE")
        return tuple(reasons)

    def validate(self) -> None:
        validate_bybit_mainnet_readonly_host(self.api_host)
        for field_name, value in (
            ("local_send_time_ms", self.local_send_time_ms),
            ("local_receive_time_ms", self.local_receive_time_ms),
            ("server_time_ms", self.server_time_ms),
            ("round_trip_time_ms", self.round_trip_time_ms),
            ("uncertainty_ms", self.uncertainty_ms),
            ("worst_case_abs_clock_skew_ms", self.worst_case_abs_clock_skew_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Bybit clock preflight {field_name} must be non-negative integer ms")
        if self.local_receive_time_ms < self.local_send_time_ms:
            raise ValueError("Bybit clock preflight local clock moved backwards")
        if self.round_trip_time_ms != self.local_receive_time_ms - self.local_send_time_ms:
            raise ValueError("Bybit clock preflight RTT is inconsistent with local timestamps")
        expected_uncertainty = (self.round_trip_time_ms + 1) // 2
        if self.uncertainty_ms != expected_uncertainty:
            raise ValueError("Bybit clock preflight uncertainty is inconsistent with RTT")
        midpoint_ms = self.local_send_time_ms + self.round_trip_time_ms // 2
        if self.estimated_clock_offset_ms != self.server_time_ms - midpoint_ms:
            raise ValueError("Bybit clock preflight offset is inconsistent with timestamps")
        expected_worst_case = abs(self.estimated_clock_offset_ms) + self.uncertainty_ms
        if self.worst_case_abs_clock_skew_ms != expected_worst_case:
            raise ValueError("Bybit clock preflight worst-case skew is inconsistent")
        if self.max_safe_clock_uncertainty_ms != _MAX_SAFE_CLOCK_UNCERTAINTY_MS:
            raise ValueError("Bybit clock preflight safety threshold cannot be relaxed")
        if self.max_safe_round_trip_ms != _MAX_SAFE_ROUND_TRIP_MS:
            raise ValueError("Bybit clock preflight RTT threshold cannot be relaxed")
        if self.environment != "BYBIT_MAINNET_READONLY":
            raise ValueError("Bybit clock preflight environment is invalid")
        if self.live_mainnet_order_routing_allowed or self.order_writes_supported:
            raise ValueError("Bybit clock preflight cannot grant order writes")

    def require_ready(self) -> None:
        self.validate()
        if not self.ready:
            reasons = ",".join(self.reasons) or "BYBIT_MAINNET_CLOCK_PREFLIGHT_NOT_READY"
            raise BybitMainnetClockPreflightError(reasons)

    def to_safe_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "ready": self.ready,
            "reasons": self.reasons,
            "api_host": self.api_host,
            "server_time_ms": self.server_time_ms,
            "round_trip_time_ms": self.round_trip_time_ms,
            "estimated_clock_offset_ms": self.estimated_clock_offset_ms,
            "uncertainty_ms": self.uncertainty_ms,
            "worst_case_abs_clock_skew_ms": self.worst_case_abs_clock_skew_ms,
            "max_safe_clock_uncertainty_ms": self.max_safe_clock_uncertainty_ms,
            "max_safe_round_trip_ms": self.max_safe_round_trip_ms,
            "live_mainnet_order_routing_allowed": False,
            "order_writes_supported": False,
        }


class BybitMainnetServerTimeHttpsTransport:
    """Public GET-only server-time transport pinned to an audited Bybit mainnet host."""

    environment = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, *, host: str, timeout_seconds: float = 10.0) -> None:
        self.host = validate_bybit_mainnet_readonly_host(host)
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Bybit server-time timeout must be within (0, 60] seconds")
        self._timeout_seconds = timeout_seconds

    def get_server_time(self) -> BybitMainnetReadOnlyHttpJson:
        connection = http.client.HTTPSConnection(
            self.host,
            443,
            timeout=self._timeout_seconds,
        )
        try:
            connection.request("GET", _SERVER_TIME_PATH)
            response = connection.getresponse()
            body = response.read()
            response_headers = {key: value for key, value in response.getheaders()}
        finally:
            connection.close()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BybitRestProtocolError(
                "Bybit server-time endpoint returned invalid JSON",
                retryable_read=False,
                ambiguous_mutation=False,
                http_status=response.status,
            ) from exc
        if not isinstance(payload, dict):
            raise BybitRestProtocolError(
                "Bybit server-time response must be an object",
                retryable_read=False,
                ambiguous_mutation=False,
                http_status=response.status,
            )
        return BybitMainnetReadOnlyHttpJson(
            status_code=response.status,
            headers=response_headers,
            payload=payload,
        )


def measure_bybit_mainnet_clock_preflight(
    *,
    host: str,
    transport: BybitMainnetServerTimeTransport | None = None,
    clock_ms: Callable[[], int] | None = None,
    rest_policy: BybitRestPolicy | None = None,
    sleep_fn: SleepFn = time.sleep,
) -> BybitMainnetClockPreflight:
    """Measure Bybit server-time offset before any authenticated account read.

    The successful request's local send/receive timestamps are used to estimate server offset.
    Half the measured RTT is treated as uncertainty and added to the absolute offset. The fixed
    500 ms worst-case threshold is intentionally much tighter than Bybit's authenticated request
    validity window and cannot be relaxed through deployment configuration.
    """

    api_host = validate_bybit_mainnet_readonly_host(host)
    active_policy = BybitRestPolicy() if rest_policy is None else rest_policy
    active_policy.validate()
    active_clock = (lambda: int(time.time() * 1000)) if clock_ms is None else clock_ms
    active_transport = (
        BybitMainnetServerTimeHttpsTransport(
            host=api_host,
            timeout_seconds=active_policy.request_timeout_seconds,
        )
        if transport is None
        else transport
    )

    def _request_once() -> tuple[int, int, int]:
        local_send_ms = _read_clock_ms(active_clock)
        raw_response = active_transport.get_server_time()
        local_receive_ms = _read_clock_ms(active_clock)
        if local_receive_ms < local_send_ms:
            raise BybitMainnetClockPreflightError(
                "local clock moved backwards during Bybit server-time request"
            )
        response = _normalize_server_time_response(raw_response)
        raise_for_bybit_response(
            status_code=response.status_code,
            headers=response.headers,
            payload=response.payload,
            mutation=False,
        )
        server_time_ms = _parse_server_time_ms(response.payload)
        return local_send_ms, local_receive_ms, server_time_ms

    local_send_ms, local_receive_ms, server_time_ms = run_bybit_read_with_retry(
        _request_once,
        policy=active_policy,
        sleep_fn=sleep_fn,
        clock_ms=active_clock,
    )
    round_trip_time_ms = local_receive_ms - local_send_ms
    midpoint_ms = local_send_ms + round_trip_time_ms // 2
    offset_ms = server_time_ms - midpoint_ms
    uncertainty_ms = (round_trip_time_ms + 1) // 2
    preflight = BybitMainnetClockPreflight(
        api_host=api_host,
        local_send_time_ms=local_send_ms,
        local_receive_time_ms=local_receive_ms,
        server_time_ms=server_time_ms,
        round_trip_time_ms=round_trip_time_ms,
        estimated_clock_offset_ms=offset_ms,
        uncertainty_ms=uncertainty_ms,
        worst_case_abs_clock_skew_ms=abs(offset_ms) + uncertainty_ms,
    )
    preflight.validate()
    return preflight


def _normalize_server_time_response(
    response: Mapping[str, Any] | BybitMainnetReadOnlyHttpJson,
) -> BybitMainnetReadOnlyHttpJson:
    if isinstance(response, BybitMainnetReadOnlyHttpJson):
        return response
    if not isinstance(response, Mapping):
        raise BybitRestProtocolError(
            "Bybit server-time transport returned invalid response type",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return BybitMainnetReadOnlyHttpJson(
        status_code=200,
        headers={},
        payload=dict(response),
    )


def _parse_server_time_ms(payload: Mapping[str, Any]) -> int:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise BybitRestProtocolError(
            "Bybit server-time result must be an object",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    raw_seconds = result.get("timeSecond")
    raw_nanos = result.get("timeNano")
    if isinstance(raw_seconds, bool) or isinstance(raw_nanos, bool):
        raise BybitRestProtocolError(
            "Bybit server-time fields must be numeric strings",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    try:
        seconds = int(str(raw_seconds))
        nanos = int(str(raw_nanos))
    except (TypeError, ValueError) as exc:
        raise BybitRestProtocolError(
            "Bybit server-time fields are invalid",
            retryable_read=False,
            ambiguous_mutation=False,
        ) from exc
    if seconds < 0 or nanos < 0:
        raise BybitRestProtocolError(
            "Bybit server-time fields cannot be negative",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    server_time_ms = nanos // 1_000_000
    if server_time_ms // 1000 != seconds:
        raise BybitRestProtocolError(
            "Bybit server-time second/nanosecond fields disagree",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return server_time_ms


def _read_clock_ms(clock_ms: Callable[[], int]) -> int:
    value = clock_ms()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BybitMainnetClockPreflightError(
            "local clock must return a non-negative integer millisecond value"
        )
    return value
