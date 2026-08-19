from __future__ import annotations

import pytest

from app.execution.bybit_private_stream import (
    BybitPrivateStreamMonitor,
    BybitPrivateStreamProtocolError,
    BybitPrivateWebsocketsConnector,
    _auth_signature,
)


class _SafeConnector:
    live_mainnet_order_routing_allowed = False

    def __call__(self, _url, *, timeout_seconds):
        del timeout_seconds
        raise AssertionError("network connection is not expected in this unit test")


def _monitor() -> BybitPrivateStreamMonitor:
    return BybitPrivateStreamMonitor(
        api_key="demo-key",
        api_secret="secret",
        connector=_SafeConnector(),
        clock_ms=lambda: 1_700_000_000_000,
        monotonic_fn=lambda: 100.0,
    )


def test_auth_signature_matches_bybit_v5_contract() -> None:
    assert _auth_signature("secret", 1_700_000_010_000) == (
        "7ccf9bb4db01ad0e1ee2c3eef8d8b4730fc9bcab069e44972f6723cde7f692f3"
    )


def test_trade_events_create_reconciliation_tokens_without_seq_continuity_assumption() -> None:
    monitor = _monitor()

    monitor._handle_topic_frame(  # noqa: SLF001 - protocol contract unit test.
        {
            "topic": "execution",
            "data": [{"symbol": "BTCUSDT", "seq": 100}],
        }
    )
    first = monitor.snapshot()
    assert first.reconciliation_required is True
    assert first.reconciliation_token == 1

    monitor._handle_topic_frame(  # noqa: SLF001 - same seq is valid across linked updates.
        {
            "topic": "position",
            "data": [{"symbol": "BTCUSDT", "seq": 100}],
        }
    )
    second = monitor.snapshot()
    assert second.reconciliation_token == 2
    assert monitor.acknowledge_reconciliation(token=first.reconciliation_token) is False
    assert monitor.acknowledge_reconciliation(token=second.reconciliation_token) is True
    assert monitor.snapshot().reconciliation_required is False


def test_duplicate_order_notifications_are_only_reconciliation_wakeups() -> None:
    monitor = _monitor()
    frame = {
        "topic": "order",
        "data": [{"symbol": "BTCUSDT", "orderStatus": "Filled"}],
    }

    monitor._handle_topic_frame(frame)  # noqa: SLF001 - protocol contract unit test.
    monitor._handle_topic_frame(frame)  # noqa: SLF001 - duplicate broker events are allowed.

    snapshot = monitor.snapshot()
    assert snapshot.reconciliation_required is True
    assert snapshot.reconciliation_token == 2


def test_invalid_private_stream_payload_fails_closed() -> None:
    monitor = _monitor()

    with pytest.raises(BybitPrivateStreamProtocolError, match="topic data"):
        monitor._handle_topic_frame(  # noqa: SLF001 - protocol contract unit test.
            {"topic": "execution", "data": {"symbol": "BTCUSDT", "seq": 1}}
        )

    with pytest.raises(BybitPrivateStreamProtocolError, match="symbol"):
        monitor._handle_topic_frame(  # noqa: SLF001 - protocol contract unit test.
            {"topic": "position", "data": [{"symbol": "btcusdt", "seq": 1}]}
        )

    with pytest.raises(BybitPrivateStreamProtocolError, match="sequence"):
        monitor._handle_topic_frame(  # noqa: SLF001 - protocol contract unit test.
            {"topic": "execution", "data": [{"symbol": "BTCUSDT", "seq": True}]}
        )


def test_connector_and_monitor_refuse_non_demo_private_endpoint() -> None:
    connector = BybitPrivateWebsocketsConnector()
    with pytest.raises(BybitPrivateStreamProtocolError, match="restricted to Bybit demo"):
        connector("wss://stream.bybit.com/v5/private", timeout_seconds=1)

    with pytest.raises(ValueError, match="must remain Bybit demo"):
        BybitPrivateStreamMonitor(
            api_key="key",
            api_secret="secret",
            url="wss://stream.bybit.com/v5/private",
            connector=_SafeConnector(),
        )
