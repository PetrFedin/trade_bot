from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.domain.trading import Bar
from app.strategy.backtest import BacktestConfig
from app.strategy.momentum import LongOnlyMomentumStrategy
from app.strategy.qualification import WalkForwardPolicy, WalkForwardQualifier

UTC = timezone.utc
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


def series(values: list[int]) -> list[Bar]:
    return [
        Bar("AAPL", START + timedelta(minutes=index), Decimal(value))
        for index, value in enumerate(values)
    ]


def qualifier(**policy_changes) -> WalkForwardQualifier:
    policy = WalkForwardPolicy(
        training_bars=6,
        testing_bars=3,
        step_bars=3,
        minimum_windows=3,
        maximum_drawdown_fraction=Decimal("0.10"),
        minimum_mean_oos_return=Decimal("-1"),
        minimum_mean_excess_return=Decimal("-1"),
        **policy_changes,
    )
    return WalkForwardQualifier(
        strategy=LongOnlyMomentumStrategy(target_quantity=Decimal("1")),
        backtest_config=BacktestConfig(
            opening_cash=Decimal("10000"),
            fee_per_fill=Decimal("0.10"),
            slippage_bps=Decimal("5"),
        ),
        policy=policy,
    )


def test_walk_forward_uses_multiple_strictly_future_execution_windows() -> None:
    result = qualifier().qualify(series(list(range(100, 130))))
    assert result.qualified
    assert len(result.windows) >= 3
    assert result.windows[0].training_start == 0
    assert result.windows[0].execution_start == 6
    assert result.windows[1].execution_start == 9
    assert all(window.execution_end > window.execution_start for window in result.windows)
    assert result.total_trades > 0


def test_benchmark_threshold_can_reject_validation_strategy() -> None:
    result = qualifier(minimum_mean_excess_return=Decimal("0")).qualify(
        series(list(range(100, 130)))
    )
    assert not result.qualified
    assert "MEAN_EXCESS_RETURN_BELOW_THRESHOLD" in result.reasons


def test_insufficient_history_never_qualifies() -> None:
    result = qualifier().qualify(series([100, 101, 102, 103, 104, 105, 106, 107]))
    assert not result.qualified
    assert result.reasons == ("INSUFFICIENT_WALK_FORWARD_HISTORY",)


def test_policy_requires_minimum_number_of_independent_windows() -> None:
    result = qualifier(minimum_windows=10).qualify(series(list(range(100, 130))))
    assert not result.qualified
    assert "INSUFFICIENT_WALK_FORWARD_WINDOWS" in result.reasons
