from decimal import Decimal

from app.observability.bybit_runtime_health import (
    BybitOperationalMeasurements,
    build_bybit_operational_health,
)


def _measurements(*, cash_mismatch: str) -> BybitOperationalMeasurements:
    return BybitOperationalMeasurements(
        market_data_age_seconds=Decimal("1"),
        stream_silence_seconds=Decimal("2"),
        broker_latency_ms=Decimal("50"),
        broker_error_fraction=Decimal("0"),
        uncertain_orders=0,
        reconciliation_age_seconds=Decimal("3"),
        cash_mismatch=Decimal(cash_mismatch),
        position_mismatches=0,
        daily_pnl=Decimal("0"),
        drawdown=Decimal("0"),
        kill_switch_engaged=False,
        market_data_ready=True,
        stream_ready=True,
        broker_connected=True,
        portfolio_reconciled=True,
    )


def test_measured_cash_mismatch_above_tolerance_blocks_readiness() -> None:
    report = build_bybit_operational_health(_measurements(cash_mismatch="0.75"))

    assert report.measurement_complete is True
    assert report.readiness is not None
    assert report.readiness.ready_for_paper_operation is False
    assert "CASH_MISMATCH" in report.blockers
    assert report.live_mainnet_order_routing_allowed is False


def test_measured_cash_mismatch_at_tolerance_remains_ready() -> None:
    report = build_bybit_operational_health(_measurements(cash_mismatch="0.01"))

    assert report.measurement_complete is True
    assert report.readiness is not None
    assert report.readiness.ready_for_paper_operation is True
    assert "CASH_MISMATCH" not in report.blockers
