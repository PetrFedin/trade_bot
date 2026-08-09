from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import threading


class Fault(str, Enum):
    TIMEOUT = "TIMEOUT"
    CONNECTION_RESET = "CONNECTION_RESET"
    RATE_LIMIT = "RATE_LIMIT"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"


class InjectedRateLimit(RuntimeError):
    pass


class InjectedServiceUnavailable(RuntimeError):
    pass


class InjectedMalformedResponse(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    fault: Fault
    operation: str | None = None

    def matches(self, operation: str) -> bool:
        return self.operation is None or self.operation == operation


class ChaosInjector:
    """Deterministic operation-level network fault injector."""

    def __init__(self, steps: list[Step] | tuple[Step, ...]) -> None:
        self._steps = deque(steps)
        self.attempts: list[str] = []
        self._lock = threading.RLock()

    def call(self, operation: str, target: Callable[[], object]) -> object:
        with self._lock:
            self.attempts.append(operation)
            step = self._steps[0] if self._steps else None
            if step is not None and step.matches(operation):
                self._steps.popleft()
                if step.fault is Fault.TIMEOUT:
                    raise TimeoutError("injected timeout")
                if step.fault is Fault.CONNECTION_RESET:
                    raise OSError("injected connection reset")
                if step.fault is Fault.RATE_LIMIT:
                    raise InjectedRateLimit("injected rate limit")
                if step.fault is Fault.SERVICE_UNAVAILABLE:
                    raise InjectedServiceUnavailable("injected unavailable")
                if step.fault is Fault.MALFORMED_RESPONSE:
                    raise InjectedMalformedResponse("injected malformed response")
            return target()
