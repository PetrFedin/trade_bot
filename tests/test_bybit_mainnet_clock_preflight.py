from collections.abc import Mapping
from typing import Any

import pytest

from app.execution.bybit_mainnet_clock_preflight import (
    BybitMainnetClockPreflightError,
    measure_bybit_mainnet_clock_preflight,
)
from app.execution.bybit_mainnet_readonly import BybitMainnetReadOnlyError
from app.execution.bybit_rest_policy import BybitRestProtocolError


class _ServerTimeTransport:
    def __init__(self, *, server_time_ms: int) -> None:
        self.server_time_ms = server_time_ms
        self.calls = 0

    def get_server_time(self) -> Mapping[str, Any]:
        self.calls += 1
        seconds = self.server_time_ms // 1000
        nanos = self.server_time_ms * 1_000_000
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "timeSecond": str(seconds),
                "timeNano": str(nanos),
            },
        }


class _SequenceClock:
    def __init__(self, values: list[int]) -> None:
        self.values = values

    def __call__(self) -> int:
        return self.values.pop(0)


def test_clock_preflight_uses_midpoint_and_half_rtt_uncertainty() -> None:
    transport = _ServerTimeTransport(server_time_ms=10_100)
    preflight = measure_bybit_mainnet_clock_preflight(
        host="api.bybit.com",
        transport=transport,
        clock_ms=_SequenceClock([10_000, 10_200]),
    )

    assert transport.calls == 1
    assert preflight.round_trip_time_ms == 200
    assert preflight.estimated_clock_offset_ms == 0
    assert preflight.uncertainty_ms == 100
    assert preflight.worst_case_abs_clock_skew_ms == 100
    assert preflight.ready is True
    assert preflight.reasons == ()
    assert preflight.live_mainnet_order_routing_allowed is False
    assert preflight.order_writes_supported is False


def test_clock_preflight_fails_closed_when_worst_case_skew_exceeds_500ms() -> None:
    transport = _ServerTimeTransport(server_time_ms=10_700)
    preflight = measure_bybit_mainnet_clock_preflight(
        host="api.bybit.com",
        transport=transport,
        clock_ms=_SequenceClock([10_000, 10_200]),
    )

    assert preflight.estimated_clock_offset_ms == 600
    assert preflight.uncertainty_ms == 100
    assert preflight.worst_case_abs_clock_skew_ms == 700
    assert preflight.ready is False
    assert preflight.reasons == ("BYBIT_MAINNET_CLOCK_SKEW_UNSAFE",)
    with pytest.raises(BybitMainnetClockPreflightError, match="CLOCK_SKEW_UNSAFE"):
        preflight.require_ready()


def test_clock_preflight_fails_closed_on_slow_round_trip() -> None:
    transport = _ServerTimeTransport(server_time_ms=10_750)
    preflight = measure_bybit_mainnet_clock_preflight(
        host="api.bybit.com",
        transport=transport,
        clock_ms=_SequenceClock([10_000, 11_500]),
    )

    assert preflight.round_trip_time_ms == 1500
    assert preflight.estimated_clock_offset_ms == 0
    assert preflight.uncertainty_ms == 750
    assert preflight.ready is False
    assert preflight.reasons == (
        "BYBIT_MAINNET_CLOCK_SKEW_UNSAFE",
        "BYBIT_MAINNET_CLOCK_RTT_UNSAFE",
    )


def test_clock_preflight_rejects_arbitrary_host_before_transport() -> None:
    transport = _ServerTimeTransport(server_time_ms=10_100)
    with pytest.raises(BybitMainnetReadOnlyError, match="regional allowlist"):
        measure_bybit_mainnet_clock_preflight(
            host="evil.example",
            transport=transport,
            clock_ms=_SequenceClock([10_000, 10_200]),
        )
    assert transport.calls == 0


def test_clock_preflight_rejects_inconsistent_server_time_fields() -> None:
    class _BadTransport:
        def get_server_time(self) -> Mapping[str, Any]:
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "timeSecond": "10",
                    "timeNano": "12000000000",
                },
            }

    with pytest.raises(BybitRestProtocolError, match="disagree"):
        measure_bybit_mainnet_clock_preflight(
            host="api.bybit.com",
            transport=_BadTransport(),
            clock_ms=_SequenceClock([10_000, 10_200]),
        )


def test_clock_preflight_rejects_local_clock_going_backwards() -> None:
    with pytest.raises(BybitMainnetClockPreflightError, match="moved backwards"):
        measure_bybit_mainnet_clock_preflight(
            host="api.bybit.com",
            transport=_ServerTimeTransport(server_time_ms=10_000),
            clock_ms=_SequenceClock([10_200, 10_100]),
        )


def test_clock_preflight_safety_thresholds_cannot_be_relaxed() -> None:
    preflight = measure_bybit_mainnet_clock_preflight(
        host="api.bybit.nl",
        transport=_ServerTimeTransport(server_time_ms=10_100),
        clock_ms=_SequenceClock([10_000, 10_200]),
    )
    mutated = preflight.__class__(
        **{
            **preflight.__dict__,
            "max_safe_clock_uncertainty_ms": 5000,
        }
    )
    with pytest.raises(ValueError, match="cannot be relaxed"):
        mutated.validate()
