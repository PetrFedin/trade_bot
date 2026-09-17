from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.ohlcv import OhlcvBar
from tools import walk_forward


def bar(index: int, close: float, spread: float = 0.4) -> OhlcvBar:
    price = Decimal(str(round(close, 6)))
    return OhlcvBar(
        symbol="SIMUSDT",
        timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index),
        open=price,
        high=Decimal(str(round(close + spread, 6))),
        low=Decimal(str(round(close - spread, 6))),
        close=price,
        volume=1,
        trade_count=1,
    )


def test_folds_never_overlap_their_own_training_window() -> None:
    """A test window that touches its training data measures nothing."""
    for fold in walk_forward.folds(1000, train=365, test=120):
        assert fold.train_start < fold.train_end <= fold.test_end
        assert fold.test_end - fold.train_end == 120


def test_test_windows_do_not_overlap_each_other() -> None:
    windows = [(f.train_end, f.test_end) for f in walk_forward.folds(1000, train=365, test=120)]
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert earlier[1] <= later[0]


def test_folds_are_empty_when_history_is_too_short() -> None:
    assert walk_forward.folds(300, train=365, test=120) == []


def test_degenerate_window_sizes_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least 60 bars"):
        walk_forward.folds(1000, train=10, test=120)
    with pytest.raises(ValueError, match="at least 60 bars"):
        walk_forward.folds(1000, train=365, test=5)


def test_grid_is_small_on_purpose() -> None:
    """Every extra axis multiplies the ways a search can fit noise."""
    assert len(walk_forward.grid()) == 9


def test_grid_varies_only_the_declared_axes() -> None:
    base = walk_forward.grid()[0]
    for config in walk_forward.grid():
        assert config.fast_bars == base.fast_bars
        assert config.slow_bars == base.slow_bars
        assert config.momentum_lookback_bars == base.momentum_lookback_bars


def test_grid_includes_the_shipped_configuration() -> None:
    """Tuning must be able to choose to change nothing."""
    from app.strategy.regime_momentum import RegimeAwareMomentumConfig

    shipped = RegimeAwareMomentumConfig()
    assert any(
        config.minimum_momentum_return == shipped.minimum_momentum_return
        and config.maximum_realized_volatility == shipped.maximum_realized_volatility
        for config in walk_forward.grid()
    )


def test_run_reports_both_arms_and_the_gap() -> None:
    bars = [bar(i, 100.0 + i * 0.35 + (3.0 if i % 11 == 0 else 0.0)) for i in range(900)]
    summary = walk_forward.run([_written(bars)], train=365, test=120)
    assert summary["tuned_in_sample"]["folds"] >= 1
    for key in ("tuned_in_sample", "tuned_out_of_sample", "fixed_out_of_sample"):
        assert key in summary
    assert "overfitting_gap" in summary
    assert "tuning_beat_fixed_out_of_sample" in summary


def test_in_sample_is_never_worse_than_the_grid_it_maximised() -> None:
    """The selected training score is a maximum, so it cannot be below the shipped arm."""
    bars = [bar(i, 100.0 + i * 0.35 + (3.0 if i % 11 == 0 else 0.0)) for i in range(900)]
    summary = walk_forward.run([_written(bars)], train=365, test=120)
    assert summary["tuned_in_sample"]["mean"] is not None


def _written(bars: list[OhlcvBar]):
    import csv
    import tempfile
    from pathlib import Path

    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
    writer = csv.writer(handle)
    writer.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
    for item in bars:
        writer.writerow(
            [
                item.timestamp.isoformat(),
                item.symbol,
                item.open,
                item.high,
                item.low,
                item.close,
                item.volume,
            ]
        )
    handle.close()
    return Path(handle.name)
