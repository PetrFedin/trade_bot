from decimal import Decimal

import pytest

from app.observability.readiness import (
    OperationalReadinessEvaluator,
    OperationalSnapshot,
)


def healthy(**changes) -> OperationalSnapshot:
    values = {
        "market_data_age_seconds": Decimal("1"),
        "stream_silence_seconds": Decimal("2"),
        "broker_latency_ms": Decimal("50"),
        "broker_error_fraction": Decimal("0"),
        "uncertain_orders": 0,
        "reconciliation_age_seconds": Decimal("5"),
        "cash_mismatch": Decimal("0"),
        "position_mismatches": 0,
        "daily_pnl": Decimal("10"),
        "drawdown": Decimal("20"),
        "kill_switch_engaged": False,
        "market_data_ready": True,
        "stream_ready": True,
        "broker_connected": True,
        "portfolio_reconciled": True,
    }
    values.update(changes)
    return OperationalSnapshot(**values)


def test_healthy_paper_snapshot_is_ready_but_never_enables_live() -> None:
    result = OperationalReadinessEvaluator().evaluate(healthy())
    assert result.ready_for_paper_operation
    assert not result.degraded
    assert result.reasons == ()
    assert not result.live_trading_allowed


def test_all_major_operational_failures_are_fail_closed() -> None:
    result = OperationalReadinessEvaluator().evaluate(
        healthy(
            market_data_age_seconds=Decimal("30"),
            stream_silence_seconds=Decimal("60"),
            broker_latency_ms=Decimal("3000"),
            broker_error_fraction=Decimal("0.2"),
            uncertain_orders=1,
            reconciliation_age_seconds=Decimal("120"),
            cash_mismatch=Decimal("1"),
            position_mismatches=1,
            daily_pnl=Decimal("-1000"),
            drawdown=Decimal("1500"),
            kill_switch_engaged=True,
            market_data_ready=False,
            stream_ready=False,
            broker_connected=False,
            portfolio_reconciled=False,
        )
    )
    assert not result.ready_for_paper_operation
    assert result.degraded
    assert set(result.reasons) == {
        "BROKER_DISCONNECTED",
        "BROKER_ERROR_SLO_BREACH",
        "BROKER_LATENCY_SLO_BREACH",
        "CASH_MISMATCH",
        "DAILY_LOSS_SLO_BREACH",
        "DRAWDOWN_SLO_BREACH",
        "KILL_SWITCH_ENGAGED",
        "MARKET_DATA_NOT_READY",
        "MARKET_DATA_STALE",
        "PORTFOLIO_NOT_RECONCILED",
        "POSITION_MISMATCH",
        "RECONCILIATION_STALE",
        "TRADE_STREAM_NOT_READY",
        "TRADE_STREAM_SILENT",
        "UNCERTAIN_ORDERS_PRESENT",
    }


def test_qualification_snapshot_cannot_smuggle_live_enablement() -> None:
    with pytest.raises(ValueError, match="cannot enable external/live routing"):
        OperationalReadinessEvaluator().evaluate(
            healthy(live_trading_allowed=True)
        )
