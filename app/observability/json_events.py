from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TextIO

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "api_secret",
    "authorization",
    "database_url",
    "dsn",
    "owner_token",
    "password",
    "secret",
    "signature",
)
Clock = Callable[[], datetime]


@dataclass
class StructuredJsonEventLogger:
    """Dependency-free JSON process logger; never a trading authority or audit store."""

    level: str = "INFO"
    component: str = "astra-bybit-product"
    stream: TextIO = field(default_factory=lambda: sys.stderr, repr=False)
    clock: Clock = field(default=lambda: datetime.now(UTC), repr=False)

    def __post_init__(self) -> None:
        self.level = self.level.strip().upper()
        if self.level not in _LEVELS:
            raise ValueError("structured log level is invalid")
        if not self.component.strip():
            raise ValueError("structured log component is required")

    def emit(
        self,
        level: str,
        event: str,
        *,
        fields: Mapping[str, object] | None = None,
    ) -> None:
        normalized_level = level.strip().upper()
        if normalized_level not in _LEVELS:
            raise ValueError("structured event level is invalid")
        if _LEVELS[normalized_level] < _LEVELS[self.level]:
            return
        normalized_event = event.strip().upper()
        if not normalized_event or not normalized_event.replace("_", "").isalnum():
            raise ValueError("structured event name must be normalized enum text")
        timestamp = self.clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("structured logger clock must be timezone-aware")
        record: dict[str, Any] = {
            "timestamp": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "level": normalized_level,
            "component": self.component,
            "event": normalized_event,
        }
        if fields:
            record["fields"] = _sanitize(dict(fields))
        print(
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
            file=self.stream,
            flush=True,
        )


def _sanitize(value: object, *, key: str | None = None) -> object:
    if key is not None and _sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _sensitive_key(key: str) -> bool:
    normalized = key.strip().lower()
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)
