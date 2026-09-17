from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.position_management import PositionManagementPolicy
from tools import replay_episodes as replay

POLICY = PositionManagementPolicy()


def bar(index: int, close: float, *, high: float | None = None, low: float | None = None):
    price = Decimal(str(close))
    return OhlcvBar(
        symbol="SIMUSDT",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
        open=price,
        high=Decimal(str(high if high is not None else close)),
        low=Decimal(str(low if low is not None else close)),
        close=price,
        volume=1,
        trade_count=1,
    )


def test_bar_sigma_is_zero_for_a_flat_series() -> None:
    assert replay.bar_sigma([bar(i, 100.0) for i in range(10)]) == 0.0


def test_bar_sigma_grows_with_dispersion() -> None:
    calm = [bar(i, 100.0 + (i % 2)) for i in range(40)]
    wild = [bar(i, 100.0 + 10 * (i % 2)) for i in range(40)]
    assert replay.bar_sigma(wild) > replay.bar_sigma(calm)


def test_fixed_outcome_reports_the_target_when_only_the_target_is_reached() -> None:
    bars = [bar(0, 100.0), bar(1, 100.0, high=105.0, low=99.5)]
    assert replay._fixed_outcome(bars, 0, Decimal("100"), POLICY, 5) == "target"


def test_fixed_outcome_reports_the_stop_when_only_the_stop_is_reached() -> None:
    bars = [bar(0, 100.0), bar(1, 100.0, high=100.5, low=97.0)]
    assert replay._fixed_outcome(bars, 0, Decimal("100"), POLICY, 5) == "stop"


def test_fixed_outcome_resolves_an_ambiguous_bar_to_the_stop() -> None:
    """A bar spanning both levels must resolve conservatively, as production does."""
    bars = [bar(0, 100.0, high=105.0, low=97.0)]
    assert replay._fixed_outcome(bars, 0, Decimal("100"), POLICY, 5) == "stop"


def test_fixed_outcome_reports_neither_when_the_range_is_narrow() -> None:
    bars = [bar(i, 100.0, high=100.3, low=99.7) for i in range(5)]
    assert replay._fixed_outcome(bars, 0, Decimal("100"), POLICY, 5) == "neither"


def test_the_two_readings_are_reported_separately() -> None:
    """The whole point of this harness: the ambiguous field gets both readings."""
    bars = [bar(i, 100.0 + i * 0.4, high=100.0 + i * 0.4 + 0.2, low=100.0 + i * 0.4 - 0.2)
            for i in range(80)]
    report = replay.replay(bars)
    assert "under_shipped_policy" in report and "under_fixed_levels" in report
    for block in (report["under_shipped_policy"], report["under_fixed_levels"]):
        assert set(block) == {"target_first", "stop_first", "neither", "resolved",
                              "target_first_rate"}


def test_a_flat_market_produces_no_eligible_signal() -> None:
    """The entry demands momentum; a flat series must not trade."""
    report = replay.replay([bar(i, 100.0) for i in range(80)])
    assert report["eligible_signals"] == 0
    assert report["episodes"] == 0


def test_costs_are_charged_to_every_episode() -> None:
    bars = [bar(i, 100.0 + i * 0.5, high=100.0 + i * 0.5 + 0.3, low=100.0 + i * 0.5 - 0.3)
            for i in range(120)]
    free = replay.replay(bars, cost_fraction=Decimal("0"))
    charged = replay.replay(bars, cost_fraction=Decimal("0.01"))
    if free["episodes"]:
        assert charged["realised"]["mean_return_per_episode"] < (
            free["realised"]["mean_return_per_episode"]
        )


def test_load_bars_round_trips_a_written_dataset(tmp_path: Path) -> None:
    path = tmp_path / "SIMUSDT_D.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        writer.writerow(["2026-01-01T00:00:00+00:00", "SIMUSDT", "100", "101", "99", "100.5", "12"])
    bars = replay.load_bars(path)
    assert len(bars) == 1
    assert bars[0].high == Decimal("101")


def test_replay_is_deterministic() -> None:
    bars = [bar(i, 100.0 + i * 0.3, high=100.0 + i * 0.3 + 0.4, low=100.0 + i * 0.3 - 0.4)
            for i in range(100)]
    assert replay.replay(bars) == replay.replay(bars)


def test_entry_never_uses_the_close_that_produced_the_signal() -> None:
    """Acting on the bar that generated the signal would be lookahead."""
    bars = [bar(i, 100.0 + i * 0.5, high=100.0 + i * 0.5 + 0.3, low=100.0 + i * 0.5 - 0.3)
            for i in range(120)]
    report = replay.replay(bars)
    assert report["episodes"] >= 0
    source = Path(replay.__file__).read_text()
    assert "entry_index = index + 1" in source


def test_too_short_a_series_is_rejected() -> None:
    with pytest.raises(ValueError):
        replay.replay([bar(i, 100.0 + i) for i in range(5)])


def test_random_entries_ignore_the_signal() -> None:
    """The control arm must enter where the signal would not, or it controls nothing."""
    bars = [bar(i, 100.0, high=100.4, low=99.6) for i in range(200)]
    signalled = replay.replay(bars)
    controlled = replay.replay(bars, random_entries=20)
    assert signalled["episodes"] == 0
    assert controlled["episodes"] > 0


def test_random_entries_are_reproducible() -> None:
    bars = [bar(i, 100.0 + i * 0.3, high=100.0 + i * 0.3 + 0.4, low=100.0 + i * 0.3 - 0.4)
            for i in range(200)]
    first = replay.replay(bars, random_entries=25, seed=5)
    second = replay.replay(bars, random_entries=25, seed=5)
    assert first == second


def test_different_seeds_draw_different_entries() -> None:
    bars = [bar(i, 100.0 + i * 0.3, high=100.0 + i * 0.3 + 0.4, low=100.0 + i * 0.3 - 0.4)
            for i in range(300)]
    first = replay.replay(bars, random_entries=30, seed=1)
    second = replay.replay(bars, random_entries=30, seed=2)
    assert first != second


def test_random_entries_respect_the_exit_policy() -> None:
    """Only the entry is replaced; every exit rule must still apply."""
    bars = [bar(i, 100.0 + i * 0.3, high=100.0 + i * 0.3 + 0.4, low=100.0 + i * 0.3 - 0.4)
            for i in range(300)]
    report = replay.replay(bars, random_entries=30)
    assert report["under_shipped_policy"]["resolved"] + report["under_shipped_policy"]["neither"] \
        == report["episodes"]
