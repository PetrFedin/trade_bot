from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.strategy.crypto_prospective_liquidation_context import (
    LiquidationPoint,
    LiquidationStatusPoint,
    assess_single_subscription_coverage,
    build_prospective_liquidation_context,
)

_SEED = "a" * 64
_SOURCE = "b" * 64
_SUBSCRIPTION = "c" * 64


def _signal() -> datetime:
    return datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _continuous_statuses() -> tuple[LiquidationStatusPoint, ...]:
    signal = _signal()
    start = signal - timedelta(minutes=60)
    points = [
        LiquidationStatusPoint(
            observed_at=start - timedelta(seconds=20),
            state="HEARTBEAT",
        )
    ]
    cursor = start + timedelta(seconds=20)
    while cursor <= signal:
        points.append(LiquidationStatusPoint(observed_at=cursor, state="HEARTBEAT"))
        cursor += timedelta(seconds=20)
    return tuple(points)


def test_continuous_coverage_is_required_not_just_endpoints() -> None:
    signal = _signal()
    start = signal - timedelta(minutes=60)
    statuses = (
        LiquidationStatusPoint(start - timedelta(seconds=10), "HEARTBEAT"),
        LiquidationStatusPoint(start + timedelta(minutes=30), "HEARTBEAT"),
        LiquidationStatusPoint(signal - timedelta(seconds=10), "HEARTBEAT"),
    )

    qualified, reasons, _start_status, _end_status = (
        assess_single_subscription_coverage(
            window_start=start,
            signal_available_at=signal,
            statuses=statuses,
            maximum_status_age_seconds=60,
        )
    )

    assert qualified is False
    assert "STATUS_GAP_IN_WINDOW" in reasons


def test_disconnect_in_presignal_window_blocks_all_liquidation_metrics() -> None:
    signal = _signal()
    statuses = list(_continuous_statuses())
    statuses.append(
        LiquidationStatusPoint(
            observed_at=signal - timedelta(minutes=10),
            state="DISCONNECTED",
        )
    )
    context = build_prospective_liquidation_context(
        seed_id=_SEED,
        source_snapshot_id=_SOURCE,
        symbol="BTCUSDT",
        side="LONG",
        signal_available_at=signal,
        evaluated_at=signal + timedelta(minutes=2),
        coverage_subscription_id=_SUBSCRIPTION,
        coverage_statuses=tuple(statuses),
        events=(),
    )

    assert context.coverage_qualified is False
    assert "DISCONNECT_IN_WINDOW" in context.coverage_reason_codes
    assert all(window.event_count is None for window in context.windows)
    assert all(window.known_zero is False for window in context.windows)


def test_qualified_no_event_window_is_known_zero() -> None:
    signal = _signal()
    context = build_prospective_liquidation_context(
        seed_id=_SEED,
        source_snapshot_id=_SOURCE,
        symbol="BTCUSDT",
        side="LONG",
        signal_available_at=signal,
        evaluated_at=signal + timedelta(minutes=2),
        coverage_subscription_id=_SUBSCRIPTION,
        coverage_statuses=_continuous_statuses(),
        events=(),
    )

    assert context.coverage_qualified is True
    assert context.coverage_reason_codes == ()
    assert [window.event_count for window in context.windows] == [0, 0, 0]
    assert all(window.known_zero for window in context.windows)
    assert context.trade_actionable is False
    assert context.strategy_promotion_allowed is False
    assert context.bybit_live_order_routing_allowed is False


def test_presignal_windows_aggregate_long_short_pressure_without_future_data() -> None:
    signal = _signal()
    events = (
        LiquidationPoint(
            event_id="1" * 64,
            event_time=signal - timedelta(minutes=55),
            liquidated_position_side="SHORT",
            estimated_notional_usdt=Decimal("100"),
        ),
        LiquidationPoint(
            event_id="2" * 64,
            event_time=signal - timedelta(minutes=10),
            liquidated_position_side="LONG",
            estimated_notional_usdt=Decimal("200"),
        ),
        LiquidationPoint(
            event_id="3" * 64,
            event_time=signal - timedelta(minutes=3),
            liquidated_position_side="LONG",
            estimated_notional_usdt=Decimal("50"),
        ),
    )
    context = build_prospective_liquidation_context(
        seed_id=_SEED,
        source_snapshot_id=_SOURCE,
        symbol="BTCUSDT",
        side="SHORT",
        signal_available_at=signal,
        evaluated_at=signal + timedelta(minutes=2),
        coverage_subscription_id=_SUBSCRIPTION,
        coverage_statuses=_continuous_statuses(),
        events=events,
    )

    by_window = {window.window_minutes: window for window in context.windows}
    assert by_window[5].total_estimated_notional_usdt == Decimal("50")
    assert by_window[15].total_estimated_notional_usdt == Decimal("250")
    assert by_window[15].normalized_long_minus_short_imbalance == Decimal("1")
    assert by_window[60].total_estimated_notional_usdt == Decimal("350")
    assert by_window[60].long_minus_short_estimated_notional_usdt == Decimal("150")
    assert by_window[60].normalized_long_minus_short_imbalance == (
        Decimal("150") / Decimal("350")
    )


def test_event_at_signal_time_is_rejected_as_potential_lookahead() -> None:
    signal = _signal()
    event = LiquidationPoint(
        event_id="4" * 64,
        event_time=signal,
        liquidated_position_side="LONG",
        estimated_notional_usdt=Decimal("10"),
    )

    with pytest.raises(ValueError, match="at/after signal"):
        build_prospective_liquidation_context(
            seed_id=_SEED,
            source_snapshot_id=_SOURCE,
            symbol="BTCUSDT",
            side="LONG",
            signal_available_at=signal,
            evaluated_at=signal + timedelta(minutes=2),
            coverage_subscription_id=_SUBSCRIPTION,
            coverage_statuses=_continuous_statuses(),
            events=(event,),
        )


def test_no_subscription_keeps_missing_data_distinct_from_zero() -> None:
    signal = _signal()
    context = build_prospective_liquidation_context(
        seed_id=_SEED,
        source_snapshot_id=_SOURCE,
        symbol="ETHUSDT",
        side="LONG",
        signal_available_at=signal,
        evaluated_at=signal + timedelta(minutes=2),
        coverage_subscription_id=None,
        coverage_statuses=(),
        events=(),
    )

    assert context.coverage_qualified is False
    assert context.coverage_reason_codes == ("NO_ELIGIBLE_SUBSCRIPTION",)
    assert all(window.event_count is None for window in context.windows)
    assert all(window.known_zero is False for window in context.windows)
