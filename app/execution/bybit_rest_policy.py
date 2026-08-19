from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, TypeVar

_TRANSIENT_API_CODES = frozenset({429, 10000, 10006, 10016})
_RATE_LIMIT_API_CODES = frozenset({429, 10006})
_CLOCK_SKEW_API_CODES = frozenset({10002})

T = TypeVar("T")
SleepFn = Callable[[float], None]
ClockMs = Callable[[], int]


@dataclass(frozen=True)
class BybitRestPolicy:
    request_timeout_seconds: float = 10.0
    read_max_attempts: int = 3
    read_backoff_initial_seconds: float = 0.25
    read_backoff_max_seconds: float = 2.0

    def validate(self) -> None:
        if not 0 < self.request_timeout_seconds <= 60:
            raise ValueError("Bybit REST request timeout must be within (0, 60] seconds")
        if not 1 <= self.read_max_attempts <= 5:
            raise ValueError("Bybit REST read attempts must be within [1, 5]")
        if self.read_backoff_initial_seconds <= 0:
            raise ValueError("Bybit REST initial read backoff must be positive")
        if self.read_backoff_max_seconds < self.read_backoff_initial_seconds:
            raise ValueError("Bybit REST max read backoff must not be below initial backoff")
        if self.read_backoff_max_seconds > 10:
            raise ValueError("Bybit REST max read backoff must not exceed 10 seconds")


@dataclass(frozen=True)
class BybitRateLimitSnapshot:
    limit: int | None
    remaining: int | None
    reset_timestamp_ms: int | None


class BybitRestRequestError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        retryable_read: bool,
        ambiguous_mutation: bool,
        http_status: int | None = None,
        ret_code: int | None = None,
        ret_msg: str | None = None,
        rate_limit: BybitRateLimitSnapshot | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable_read = retryable_read
        self.ambiguous_mutation = ambiguous_mutation
        self.http_status = http_status
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.rate_limit = rate_limit


class BybitRestRateLimitError(BybitRestRequestError):
    pass


class BybitRestClockSkewError(BybitRestRequestError):
    pass


class BybitRestProtocolError(BybitRestRequestError):
    pass


class BybitRestTransportError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable_read: bool,
        ambiguous_mutation: bool,
    ) -> None:
        super().__init__(message)
        self.retryable_read = retryable_read
        self.ambiguous_mutation = ambiguous_mutation


def raise_for_bybit_response(
    *,
    status_code: int,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    mutation: bool,
) -> None:
    rate_limit = parse_rate_limit_headers(headers)
    if status_code != 200:
        retryable = status_code == 429 or 500 <= status_code <= 599
        error_type = BybitRestRateLimitError if status_code == 429 else BybitRestRequestError
        raise error_type(
            f"Bybit REST HTTP status {status_code}",
            retryable_read=retryable,
            ambiguous_mutation=mutation and 500 <= status_code <= 599,
            http_status=status_code,
            rate_limit=rate_limit,
        )

    raw_ret_code = payload.get("retCode")
    if isinstance(raw_ret_code, bool) or raw_ret_code is None:
        raise BybitRestProtocolError(
            "Bybit REST response is missing numeric retCode",
            retryable_read=False,
            ambiguous_mutation=mutation,
            rate_limit=rate_limit,
        )
    try:
        ret_code = int(str(raw_ret_code))
    except (TypeError, ValueError) as exc:
        raise BybitRestProtocolError(
            "Bybit REST response has invalid retCode",
            retryable_read=False,
            ambiguous_mutation=mutation,
            rate_limit=rate_limit,
        ) from exc
    if ret_code == 0:
        return

    raw_ret_msg = payload.get("retMsg")
    ret_msg = raw_ret_msg if isinstance(raw_ret_msg, str) else None
    if ret_code in _CLOCK_SKEW_API_CODES:
        raise BybitRestClockSkewError(
            f"Bybit REST clock/recvWindow error retCode {ret_code}",
            retryable_read=False,
            ambiguous_mutation=False,
            ret_code=ret_code,
            ret_msg=ret_msg,
            rate_limit=rate_limit,
        )
    if ret_code in _RATE_LIMIT_API_CODES:
        raise BybitRestRateLimitError(
            f"Bybit REST rate limit retCode {ret_code}",
            retryable_read=True,
            ambiguous_mutation=False,
            ret_code=ret_code,
            ret_msg=ret_msg,
            rate_limit=rate_limit,
        )
    retryable = ret_code in _TRANSIENT_API_CODES
    raise BybitRestRequestError(
        f"Bybit REST API error retCode {ret_code}",
        retryable_read=retryable,
        ambiguous_mutation=mutation and ret_code in {10000, 10016},
        ret_code=ret_code,
        ret_msg=ret_msg,
        rate_limit=rate_limit,
    )


def parse_rate_limit_headers(headers: Mapping[str, str]) -> BybitRateLimitSnapshot:
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    return BybitRateLimitSnapshot(
        limit=_optional_non_negative_int(normalized.get("x-bapi-limit")),
        remaining=_optional_non_negative_int(normalized.get("x-bapi-limit-status")),
        reset_timestamp_ms=_optional_non_negative_int(
            normalized.get("x-bapi-limit-reset-timestamp")
        ),
    )


def run_bybit_read_with_retry(
    operation: Callable[[], T],
    *,
    policy: BybitRestPolicy,
    sleep_fn: SleepFn = time.sleep,
    clock_ms: ClockMs | None = None,
) -> T:
    policy.validate()
    active_clock = (lambda: int(time.time() * 1000)) if clock_ms is None else clock_ms
    for attempt in range(1, policy.read_max_attempts + 1):
        try:
            return operation()
        except BybitRestRequestError as exc:
            if not exc.retryable_read or attempt >= policy.read_max_attempts:
                raise
            delay = _retry_delay(
                policy=policy,
                failed_attempt=attempt,
                rate_limit=exc.rate_limit,
                now_ms=active_clock(),
            )
        except (OSError, HTTPException) as exc:
            if attempt >= policy.read_max_attempts:
                raise BybitRestTransportError(
                    "Bybit REST read transport failed after bounded retries",
                    retryable_read=True,
                    ambiguous_mutation=False,
                ) from exc
            delay = _retry_delay(
                policy=policy,
                failed_attempt=attempt,
                rate_limit=None,
                now_ms=active_clock(),
            )
        sleep_fn(delay)
    raise AssertionError("unreachable Bybit REST retry loop")


def mutation_transport_error(exc: BaseException) -> BybitRestTransportError:
    return BybitRestTransportError(
        f"Bybit REST mutation transport failed:{type(exc).__name__}",
        retryable_read=False,
        ambiguous_mutation=True,
    )


def _retry_delay(
    *,
    policy: BybitRestPolicy,
    failed_attempt: int,
    rate_limit: BybitRateLimitSnapshot | None,
    now_ms: int,
) -> float:
    if isinstance(now_ms, bool) or now_ms < 0:
        raise ValueError("Bybit REST retry clock must return a non-negative integer")
    exponential = min(
        policy.read_backoff_initial_seconds * (2 ** (failed_attempt - 1)),
        policy.read_backoff_max_seconds,
    )
    if rate_limit is None or rate_limit.reset_timestamp_ms is None:
        return exponential
    reset_delay = max(0.0, (rate_limit.reset_timestamp_ms - now_ms) / 1000)
    return min(policy.read_backoff_max_seconds, max(exponential, reset_delay))


def _optional_non_negative_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
