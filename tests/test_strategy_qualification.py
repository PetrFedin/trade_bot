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
    policy_values = {
        "training_bars": 6,
        "testing_bars": 3,
        "step_bars": 3,
        "minimum_windows": 3,
        "maximum_drawdown_fraction": Decimal("0.10"),
        "minimum_mean_oos_return": Decimal("-1"),
        "minimum_mean_excess_return": Decimal("-1"),
    }
    policy_values.update(policy_changes)
    policy = WalkForwardPolicy(**policy_values)
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
    assert result.active_windows == len(result.windows)


def test_window_acceptance_benchmark_is_capital_matched_not_raw_asset_return() -> None:
    result = qualifier().qualify(series(list(range(100, 130))))
    window = result.windows[0]
    first_oos = Decimal("106")
    last_oos = Decimal("108")
    expected_capital_matched = (
        last_oos
        - first_oos * Decimal("1.0005")
        - Decimal("0.10")
    ) / Decimal("10000")
    expected_asset_return = (last_oos - first_oos) / first_oos
    assert window.benchmark_return == expected_capital_matched
    assert window.capital_matched_benchmark_return == expected_capital_matched
    assert window.asset_benchmark_return == expected_asset_return
    assert window.cash_benchmark_return == Decimal("0")
    assert window.asset_benchmark_return > window.benchmark_return
    assert window.excess_return == window.strategy_return - window.benchmark_return


def test_benchmark_threshold_can_reject_validation_strategy() -> None:
    result = qualifier(minimum_mean_excess_return=Decimal("0.000001")).qualify(
        series(list(range(100, 130)))
    )
    assert not result.qualified
    assert "MEAN_EXCESS_RETURN_BELOW_THRESHOLD" in result.reasons


def test_minimum_active_windows_rejects_no_trade_regime() -> None:
    descending = list(range(130, 100, -1))
    result = qualifier(minimum_active_windows=1).qualify(series(descending))
    assert not result.qualified
    assert result.active_windows == 0
    assert result.total_trades == 0
    assert result.reasons == ("INSUFFICIENT_ACTIVE_WINDOWS",)


def test_default_activity_policy_preserves_no_trade_qualification_semantics() -> None:
    descending = list(range(130, 100, -1))
    result = qualifier().qualify(series(descending))
    assert result.qualified
    assert result.active_windows == 0
    assert result.total_trades == 0


def test_insufficient_history_never_qualifies() -> None:
    result = qualifier().qualify(series([100, 101, 102, 103, 104, 105, 106, 107]))
    assert not result.qualified
    assert result.reasons == ("INSUFFICIENT_WALK_FORWARD_HISTORY",)
    assert result.active_windows == 0


def test_policy_requires_minimum_number_of_independent_windows() -> None:
    result = qualifier(minimum_windows=10).qualify(series(list(range(100, 130))))
    assert not result.qualified
    assert "INSUFFICIENT_WALK_FORWARD_WINDOWS" in result.reasons
