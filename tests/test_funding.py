from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools import replay_episodes as replay


def schedule(count: int, rate: float, *, start: datetime | None = None):
    base = start or datetime(2026, 1, 1, tzinfo=UTC)
    return [(base + timedelta(hours=8 * i), rate) for i in range(count)]


def test_settlement_interval_is_eight_hours() -> None:
    assert replay.FUNDING_SETTLEMENT == timedelta(hours=8)


def test_no_schedule_costs_nothing() -> None:
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    assert replay.funding_paid([], opened, opened + timedelta(days=10)) == 0.0


def test_a_position_closed_before_the_next_settlement_pays_nothing() -> None:
    """Holding across no settlement is holding for free; charging it would overstate."""
    rows = schedule(10, 0.0001)
    opened = datetime(2026, 1, 1, 1, tzinfo=UTC)
    assert replay.funding_paid(rows, opened, opened + timedelta(hours=6)) == 0.0


def test_each_settlement_inside_the_interval_is_charged_once() -> None:
    rows = schedule(10, 0.0001)
    opened = datetime(2026, 1, 1, 1, tzinfo=UTC)
    paid = replay.funding_paid(rows, opened, opened + timedelta(hours=25))
    assert paid == pytest.approx(0.0003, abs=1e-9)


def test_the_settlement_at_the_open_instant_is_not_charged() -> None:
    """A position opened at a settlement did not hold through it."""
    rows = schedule(4, 0.0001)
    assert replay.funding_paid(rows, rows[0][0], rows[0][0] + timedelta(hours=1)) == 0.0


def test_a_negative_rate_pays_the_holder() -> None:
    """When funding is negative the long receives it; the sign must survive."""
    rows = schedule(4, -0.0002)
    opened = rows[0][0] + timedelta(minutes=1)
    assert replay.funding_paid(rows, opened, opened + timedelta(hours=17)) < 0


def test_an_inverted_interval_costs_nothing() -> None:
    rows = schedule(4, 0.0001)
    later = rows[3][0]
    assert replay.funding_paid(rows, later, rows[0][0]) == 0.0


def test_longer_holds_cost_more() -> None:
    rows = schedule(40, 0.0001)
    opened = rows[0][0] + timedelta(minutes=1)
    short = replay.funding_paid(rows, opened, opened + timedelta(days=1))
    long = replay.funding_paid(rows, opened, opened + timedelta(days=5))
    assert long > short


def test_load_funding_sorts_oldest_first(tmp_path: Path) -> None:
    path = tmp_path / "SIM_funding.csv"
    path.write_text(
        "timestamp,symbol,funding_rate\n"
        "2026-01-02T00:00:00+00:00,SIM,0.0001\n"
        "2026-01-01T00:00:00+00:00,SIM,0.0002\n"
    )
    rows = replay.load_funding(path)
    assert [rate for _, rate in rows] == [0.0002, 0.0001]


def test_replay_reports_funding_as_not_applied_by_default() -> None:
    from datetime import datetime as dt
    from decimal import Decimal

    from app.marketdata.ohlcv import OhlcvBar

    bars = [
        OhlcvBar(
            symbol="SIMUSDT",
            timestamp=dt(2026, 1, 1, tzinfo=UTC) + timedelta(days=i),
            open=Decimal(str(100 + i)),
            high=Decimal(str(100.5 + i)),
            low=Decimal(str(99.5 + i)),
            close=Decimal(str(100 + i)),
            volume=1,
            trade_count=1,
        )
        for i in range(120)
    ]
    report = replay.replay(bars)
    assert report["funding"]["applied"] is False
    assert report["funding"]["total_charged"] == 0.0
