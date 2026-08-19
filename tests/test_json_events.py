from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from app.observability.json_events import StructuredJsonEventLogger


def _clock() -> datetime:
    return datetime(2026, 8, 19, 18, 0, tzinfo=UTC)


def test_structured_logger_emits_stable_json_and_redacts_sensitive_fields() -> None:
    stream = io.StringIO()
    logger = StructuredJsonEventLogger(level="INFO", stream=stream, clock=_clock)

    logger.emit(
        "INFO",
        "BYBIT_PRODUCT_STARTING",
        fields={
            "symbol": "BTCUSDT",
            "api_key": "must-not-leak",
            "nested": {
                "database_url": "postgresql://secret@db/astra",
                "safe": True,
            },
        },
    )

    payload = json.loads(stream.getvalue())
    assert payload["timestamp"] == "2026-08-19T18:00:00Z"
    assert payload["level"] == "INFO"
    assert payload["component"] == "astra-bybit-product"
    assert payload["event"] == "BYBIT_PRODUCT_STARTING"
    assert payload["fields"]["symbol"] == "BTCUSDT"
    assert payload["fields"]["api_key"] == "[REDACTED]"
    assert payload["fields"]["nested"]["database_url"] == "[REDACTED]"
    assert "must-not-leak" not in stream.getvalue()
    assert "postgresql://secret@db/astra" not in stream.getvalue()


def test_structured_logger_respects_configured_level() -> None:
    stream = io.StringIO()
    logger = StructuredJsonEventLogger(level="ERROR", stream=stream, clock=_clock)

    logger.emit("INFO", "LOW_PRIORITY_EVENT")
    logger.emit("ERROR", "FAILURE_EVENT", fields={"error_type": "RuntimeError"})

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "FAILURE_EVENT"


def test_structured_logger_rejects_naive_clock() -> None:
    logger = StructuredJsonEventLogger(
        stream=io.StringIO(),
        clock=lambda: datetime(2026, 8, 19, 18, 0),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        logger.emit("INFO", "CLOCK_TEST")
