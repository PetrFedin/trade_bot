from __future__ import annotations

from decimal import Decimal

import pytest

from app.observability.bybit_runtime_health import (
    BybitOperationalMeasurements,
    BybitRestHealthRecorder,
    build_bybit_operational_health,
)


def test_missing_measurements_fail_closed_without_operational_snapshot() -> None:
    report = build_bybit_operational_health(BybitOperationalMeasurements())

    assert report.measurement_complete is False
    assert report.snapshot is None
    assert report.readiness is None
    assert "MEASUREMENT_UNAVAILABLE:uncertain_orders" in report.blockers
    assert "MEASUREMENT_UNAVAILABLE:kill_switch_engaged" in report.blockers
    assert report.live_mainnet_order_routing_allowed is False


def test_complete_measured_health_reuses_canonical_readiness_evaluator() -> None:
    report = build_bybit_operational_health(
        BybitOperationalMeasurements(
            market_data_age_seconds=Decimal("1"),
            stream_silence_seconds=Decimal("2"),
            broker_latency_ms=Decimal("50"),
            broker_error_fraction=Decimal("0"),
            uncertain_orders=0,
            reconciliation_age_seconds=Decimal("3"),
            cash_mismatch=Decimal("0"),
            position_mismatches=0,
            daily_pnl=Decimal("10"),
            drawdown=Decimal("5"),
            kill_switch_engaged=False,
            market_data_ready=True,
            stream_ready=True,
            broker_connected=True,
            portfolio_reconciled=True,
        )
    )

    assert report.measurement_complete is True
    assert report.snapshot is not None
    assert report.readiness is not None
    assert report.readiness.ready_for_paper_operation is True
    assert report.blockers == ()
    assert report.readiness.live_trading_allowed is False
    assert report.live_mainnet_order_routing_allowed is False


def test_complete_measured_health_preserves_canonical_block_reason() -> None:
    report = build_bybit_operational_health(
        BybitOperationalMeasurements(
            market_data_age_seconds=Decimal("1"),
            stream_silence_seconds=Decimal("2"),
            broker_latency_ms=Decimal("50"),
            broker_error_fraction=Decimal("0"),
            uncertain_orders=1,
            reconciliation_age_seconds=Decimal("3"),
            cash_mismatch=Decimal("0"),
            position_mismatches=0,
            daily_pnl=Decimal("10"),
            drawdown=Decimal("5"),
            kill_switch_engaged=False,
            market_data_ready=True,
            stream_ready=True,
            broker_connected=True,
            portfolio_reconciled=True,
        )
    )

    assert report.readiness is not None
    assert report.readiness.ready_for_paper_operation is False
    assert "UNCERTAIN_ORDERS_PRESENT" in report.blockers


def test_rest_health_recorder_reports_measured_latency_and_error_fraction() -> None:
    recorder = BybitRestHealthRecorder(window_size=3)
    recorder.record(
        latency_ms=Decimal("20"),
        success=True,
        observed_monotonic=Decimal("100"),
    )
    recorder.record(
        latency_ms=Decimal("40"),
        success=False,
        observed_monotonic=Decimal("101"),
        error_type="BybitRestRateLimitError",
    )

    snapshot = recorder.snapshot()

    assert snapshot.total_calls == 2
    assert snapshot.window_calls == 2
    assert snapshot.window_errors == 1
    assert snapshot.error_fraction == Decimal("0.5")
    assert snapshot.last_latency_ms == Decimal("40")
    assert snapshot.maximum_latency_ms == Decimal("40")
    assert snapshot.last_success_monotonic == Decimal("100")
    assert snapshot.last_error_type == "BybitRestRateLimitError"


def test_rest_health_recorder_rejects_fake_success_error_metadata() -> None:
    recorder = BybitRestHealthRecorder()

    with pytest.raises(ValueError, match="successful REST observation"):
        recorder.record(
            latency_ms=Decimal("1"),
            success=True,
            observed_monotonic=Decimal("1"),
            error_type="should-not-exist",
        )
