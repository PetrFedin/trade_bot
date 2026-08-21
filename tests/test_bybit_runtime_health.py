from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.observability.bybit_runtime_health import (
    BybitAccountRiskHealthRecorder,
    BybitMarketDataHealthRecorder,
    BybitOperationalMeasurements,
    BybitReconciliationHealthRecorder,
    BybitRestHealthRecorder,
    build_bybit_operational_health,
    collect_bybit_operational_measurements,
)


@dataclass(frozen=True)
class _ReconciliationResult:
    status: str
    broker_truth_complete: bool
    live_mainnet_order_routing_allowed: bool = False


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


def test_market_data_health_is_unknown_until_real_observation_then_ages() -> None:
    recorder = BybitMarketDataHealthRecorder()

    missing = recorder.snapshot(now_monotonic=Decimal("100"))
    assert missing.market_data_ready is False
    assert missing.market_data_age_seconds is None

    recorder.record_success(observed_monotonic=Decimal("101.5"))
    observed = recorder.snapshot(now_monotonic=Decimal("104"))
    assert observed.market_data_ready is True
    assert observed.market_data_age_seconds == Decimal("2.5")


def test_account_risk_health_uses_wallet_high_water_and_authoritative_daily_pnl() -> None:
    recorder = BybitAccountRiskHealthRecorder()

    missing = recorder.snapshot()
    assert missing.drawdown_usdt is None
    assert missing.daily_pnl_usdt is None

    recorder.record(
        current_equity_usdt=Decimal("950"),
        peak_equity_usdt=Decimal("1100"),
        daily_pnl_usdt=Decimal("-17.5"),
    )
    snapshot = recorder.snapshot()

    assert snapshot.current_equity_usdt == Decimal("950")
    assert snapshot.peak_equity_usdt == Decimal("1100")
    assert snapshot.daily_pnl_usdt == Decimal("-17.5")
    assert snapshot.drawdown_usdt == Decimal("150")

    with pytest.raises(ValueError, match="peak equity cannot be below current equity"):
        recorder.record(
            current_equity_usdt=Decimal("1000"),
            peak_equity_usdt=Decimal("999"),
            daily_pnl_usdt=Decimal("0"),
        )


def test_reconciliation_health_tracks_actual_rest_truth_age_and_state() -> None:
    recorder = BybitReconciliationHealthRecorder()
    recorder.record(
        _ReconciliationResult("READY_FOR_ENTRY", True),
        observed_monotonic=Decimal("100"),
    )

    healthy = recorder.snapshot(now_monotonic=Decimal("107.5"))

    assert healthy.last_success_monotonic == Decimal("100")
    assert healthy.reconciliation_age_seconds == Decimal("7.5")
    assert healthy.broker_connected is True
    assert healthy.portfolio_reconciled is True
    assert healthy.position_mismatches == 0

    recorder.record(
        _ReconciliationResult("BLOCKED", False),
        observed_monotonic=Decimal("108"),
    )
    degraded = recorder.snapshot(now_monotonic=Decimal("110"))

    assert degraded.reconciliation_age_seconds == Decimal("10")
    assert degraded.broker_connected is False
    assert degraded.portfolio_reconciled is False
    assert degraded.position_mismatches is None


def test_collector_uses_only_proven_runtime_sources_and_leaves_account_gaps_unknown() -> None:
    rest = BybitRestHealthRecorder()
    rest.record(
        latency_ms=Decimal("30"),
        success=True,
        observed_monotonic=Decimal("100"),
    )
    rest.record(
        latency_ms=Decimal("80"),
        success=False,
        observed_monotonic=Decimal("101"),
        error_type="BybitRestRateLimitError",
    )
    market_data = BybitMarketDataHealthRecorder()
    market_data.record_success(observed_monotonic=Decimal("103"))
    reconciliation = BybitReconciliationHealthRecorder()
    reconciliation.record(
        _ReconciliationResult("READY_FOR_ENTRY", True),
        observed_monotonic=Decimal("100"),
    )
    private_stream = SimpleNamespace(
        healthy=True,
        last_message_monotonic=104.0,
        live_mainnet_order_routing_allowed=False,
    )
    operator = SimpleNamespace(
        kill_switch_engaged=False,
        live_mainnet_order_routing_allowed=False,
    )

    measurements = collect_bybit_operational_measurements(
        now_monotonic=Decimal("105"),
        market_data=market_data.snapshot(now_monotonic=Decimal("105")),
        rest=rest.snapshot(),
        reconciliation=reconciliation.snapshot(now_monotonic=Decimal("105")),
        private_stream=private_stream,
        unresolved_entry_submissions=2,
        operator=operator,
    )

    assert measurements.market_data_age_seconds == Decimal("2")
    assert measurements.market_data_ready is True
    assert measurements.stream_silence_seconds == Decimal("1.0")
    assert measurements.stream_ready is True
    assert measurements.broker_latency_ms == Decimal("80")
    assert measurements.broker_error_fraction == Decimal("0.5")
    assert measurements.uncertain_orders == 2
    assert measurements.reconciliation_age_seconds == Decimal("5")
    assert measurements.broker_connected is True
    assert measurements.portfolio_reconciled is True
    assert measurements.position_mismatches == 0
    assert measurements.kill_switch_engaged is False
    assert measurements.cash_mismatch is None
    assert measurements.daily_pnl is None
    assert measurements.drawdown is None

    report = build_bybit_operational_health(measurements)
    assert report.measurement_complete is False
    assert "MEASUREMENT_UNAVAILABLE:market_data_age_seconds" not in report.blockers
    assert "MEASUREMENT_UNAVAILABLE:market_data_ready" not in report.blockers
    assert "MEASUREMENT_UNAVAILABLE:daily_pnl" in report.blockers
    assert "MEASUREMENT_UNAVAILABLE:cash_mismatch" in report.blockers
    assert "MEASUREMENT_UNAVAILABLE:uncertain_orders" not in report.blockers
    assert "MEASUREMENT_UNAVAILABLE:kill_switch_engaged" not in report.blockers


def test_collector_adds_real_drawdown_and_daily_pnl_without_faking_cash() -> None:
    account_risk = BybitAccountRiskHealthRecorder()
    account_risk.record(
        current_equity_usdt=Decimal("950"),
        peak_equity_usdt=Decimal("1100"),
        daily_pnl_usdt=Decimal("-12"),
    )
    reconciliation = BybitReconciliationHealthRecorder().snapshot(
        now_monotonic=Decimal("1")
    )
    market_data = BybitMarketDataHealthRecorder().snapshot(now_monotonic=Decimal("1"))

    measurements = collect_bybit_operational_measurements(
        now_monotonic=Decimal("2"),
        market_data=market_data,
        rest=BybitRestHealthRecorder().snapshot(),
        reconciliation=reconciliation,
        private_stream=None,
        unresolved_entry_submissions=None,
        operator=None,
        account_risk=account_risk.snapshot(),
    )

    assert measurements.drawdown == Decimal("150")
    assert measurements.daily_pnl == Decimal("-12")
    assert measurements.cash_mismatch is None
    report = build_bybit_operational_health(measurements)
    assert "MEASUREMENT_UNAVAILABLE:drawdown" not in report.blockers
    assert "MEASUREMENT_UNAVAILABLE:daily_pnl" not in report.blockers
    assert "MEASUREMENT_UNAVAILABLE:cash_mismatch" in report.blockers


def test_collector_hard_rejects_mainnet_capable_measurement_source() -> None:
    reconciliation = BybitReconciliationHealthRecorder().snapshot(
        now_monotonic=Decimal("1")
    )
    market_data = BybitMarketDataHealthRecorder().snapshot(now_monotonic=Decimal("1"))
    stream = SimpleNamespace(
        healthy=True,
        last_message_monotonic=1.0,
        live_mainnet_order_routing_allowed=True,
    )

    with pytest.raises(ValueError, match="mainnet-capable private stream snapshot"):
        collect_bybit_operational_measurements(
            now_monotonic=Decimal("2"),
            market_data=market_data,
            rest=BybitRestHealthRecorder().snapshot(),
            reconciliation=reconciliation,
            private_stream=stream,
            unresolved_entry_submissions=0,
            operator=None,
        )
