from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_signal_event_outcomes import audit_all_crypto_signal_events


def _rising_acquisition(count: int = 100) -> BybitKlineAcquisition:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars: list[BybitKlineBar] = []
    for index in range(count):
        close = Decimal("100") + Decimal(index) * Decimal("0.5")
        bars.append(
            BybitKlineBar(
                symbol="BTCUSDT",
                start_time=start + timedelta(minutes=5 * index),
                open=close - Decimal("0.1"),
                high=close + Decimal("0.2"),
                low=close - Decimal("0.4"),
                close=close,
                volume=Decimal("10000"),
                turnover=Decimal("1000000"),
            )
        )
    return BybitKlineAcquisition(bars=tuple(bars), pages_by_symbol={"BTCUSDT": 1})


def test_all_signal_audit_is_independent_of_portfolio_slots() -> None:
    report = audit_all_crypto_signal_events(_rising_acquisition())

    assert report["signal_event_count"] > 0
    assert report["portfolio_slot_constraints_applied"] is False
    assert report["cooldown_constraints_applied"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["trade_actionable"] is False
    assert report["by_side"]["LONG"]["signal_count"] == report["signal_event_count"]


def test_all_signal_audit_reports_forward_direction_and_clarity() -> None:
    report = audit_all_crypto_signal_events(_rising_acquisition())
    aggregate = report["aggregate"]

    assert aggregate["horizons"]["15"]["complete_count"] > 0
    assert aggregate["horizons"]["15"]["positive_direction_rate"] == 1.0
    assert aggregate["median_quality_ratio"] > 1.0
    assert report["by_clarity_band"]
    assert all(
        row["quality_ratio_to_entry_gate"] >= 1.0
        for row in report["signal_rows"]
    )


def test_240m_excursion_requires_complete_contiguous_window() -> None:
    report = audit_all_crypto_signal_events(_rising_acquisition())
    rows = report["signal_rows"]

    complete = [
        row
        for row in rows
        if next(
            horizon
            for horizon in row["horizons"]
            if horizon["minutes"] == 240
        )["complete"]
    ]
    incomplete = [row for row in rows if row not in complete]

    assert complete
    assert incomplete
    assert all(row["maximum_favorable_r_240m"] is not None for row in complete)
    assert all(row["maximum_adverse_r_240m"] is not None for row in complete)
    assert all(row["maximum_favorable_r_240m"] is None for row in incomplete)
    assert all(row["maximum_adverse_r_240m"] is None for row in incomplete)


def test_all_signal_audit_exposes_reference_equity_plan_filter() -> None:
    report = audit_all_crypto_signal_events(
        _rising_acquisition(),
        reference_equity_usdt=Decimal("1000"),
    )
    aggregate = report["aggregate"]

    assert aggregate["plan_eligible_count"] <= aggregate["signal_count"]
    assert 0.0 <= aggregate["plan_eligible_rate"] <= 1.0
    assert report["reference_equity_usdt"] == 1000.0
