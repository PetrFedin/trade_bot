from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain.trading import Bar
from app.strategy.backtest import BacktestConfig, HistoricalBacktester
from app.strategy.momentum import LongOnlyMomentumStrategy


@dataclass(frozen=True)
class WalkForwardPolicy:
    training_bars: int = 20
    testing_bars: int = 5
    step_bars: int = 5
    minimum_windows: int = 3
    maximum_drawdown_fraction: Decimal = Decimal("0.10")
    minimum_mean_oos_return: Decimal = Decimal("-1")
    minimum_mean_excess_return: Decimal = Decimal("-1")
    require_trade_in_each_window: bool = False

    def validate(self) -> None:
        if self.training_bars < 3:
            raise ValueError("training_bars must be at least three")
        if self.testing_bars < 2:
            raise ValueError("testing_bars must be at least two")
        if self.step_bars < 1:
            raise ValueError("step_bars must be positive")
        if self.minimum_windows < 1:
            raise ValueError("minimum_windows must be positive")
        if (
            not self.maximum_drawdown_fraction.is_finite()
            or self.maximum_drawdown_fraction < 0
            or self.maximum_drawdown_fraction > 1
        ):
            raise ValueError("maximum_drawdown_fraction must be within [0, 1]")
        for name, value in (
            ("minimum_mean_oos_return", self.minimum_mean_oos_return),
            ("minimum_mean_excess_return", self.minimum_mean_excess_return),
        ):
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")


@dataclass(frozen=True)
class WalkForwardWindow:
    window_number: int
    training_start: int
    execution_start: int
    execution_end: int
    strategy_return: Decimal
    benchmark_return: Decimal
    excess_return: Decimal
    max_drawdown_fraction: Decimal
    trades: int


@dataclass(frozen=True)
class StrategyQualification:
    qualified: bool
    reasons: tuple[str, ...]
    windows: tuple[WalkForwardWindow, ...]
    mean_oos_return: Decimal
    mean_excess_return: Decimal
    worst_drawdown_fraction: Decimal
    total_trades: int


class WalkForwardQualifier:
    """Out-of-sample qualification for a fixed strategy implementation.

    This framework deliberately does not optimize parameters. Each window supplies a
    historical warm-up segment, then evaluates only future bars using next-bar fills.
    It therefore tests repeatability without turning the qualification set into a
    parameter-search surface.
    """

    def __init__(
        self,
        *,
        strategy: LongOnlyMomentumStrategy,
        backtest_config: BacktestConfig | None = None,
        policy: WalkForwardPolicy | None = None,
    ) -> None:
        self.policy = WalkForwardPolicy() if policy is None else policy
        self.policy.validate()
        self.backtest_config = BacktestConfig() if backtest_config is None else backtest_config
        self.backtest_config.validate()
        self.strategy = strategy

    def qualify(self, bars: Sequence[Bar]) -> StrategyQualification:
        ordered = list(bars)
        if not ordered:
            return StrategyQualification(
                qualified=False,
                reasons=("NO_BARS",),
                windows=(),
                mean_oos_return=Decimal("0"),
                mean_excess_return=Decimal("0"),
                worst_drawdown_fraction=Decimal("0"),
                total_trades=0,
            )
        for bar in ordered:
            bar.validate()
        if len({bar.symbol for bar in ordered}) != 1:
            raise ValueError("qualification requires exactly one symbol")
        timestamps = [bar.timestamp for bar in ordered]
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            raise ValueError("qualification bars must be strictly increasing")

        windows: list[WalkForwardWindow] = []
        start = 0
        number = 1
        while start + self.policy.training_bars + self.policy.testing_bars <= len(ordered):
            execution_start = start + self.policy.training_bars
            execution_end = execution_start + self.policy.testing_bars
            fold = ordered[start:execution_end]
            local_execution_start = self.policy.training_bars
            result = HistoricalBacktester(
                strategy=self.strategy,
                config=self.backtest_config,
            ).run(fold, first_execution_index=local_execution_start)
            first_oos = fold[local_execution_start]
            last_oos = fold[-1]
            benchmark_return = (last_oos.close - first_oos.close) / first_oos.close
            drawdown_fraction = result.max_drawdown / self.backtest_config.opening_cash
            windows.append(
                WalkForwardWindow(
                    window_number=number,
                    training_start=start,
                    execution_start=execution_start,
                    execution_end=execution_end,
                    strategy_return=result.total_return,
                    benchmark_return=benchmark_return,
                    excess_return=result.total_return - benchmark_return,
                    max_drawdown_fraction=drawdown_fraction,
                    trades=result.trades,
                )
            )
            start += self.policy.step_bars
            number += 1

        if not windows:
            return StrategyQualification(
                qualified=False,
                reasons=("INSUFFICIENT_WALK_FORWARD_HISTORY",),
                windows=(),
                mean_oos_return=Decimal("0"),
                mean_excess_return=Decimal("0"),
                worst_drawdown_fraction=Decimal("0"),
                total_trades=0,
            )

        count = Decimal(len(windows))
        mean_return = sum((window.strategy_return for window in windows), Decimal("0")) / count
        mean_excess = sum((window.excess_return for window in windows), Decimal("0")) / count
        worst_drawdown = max(window.max_drawdown_fraction for window in windows)
        total_trades = sum(window.trades for window in windows)
        reasons: set[str] = set()
        if len(windows) < self.policy.minimum_windows:
            reasons.add("INSUFFICIENT_WALK_FORWARD_WINDOWS")
        if mean_return < self.policy.minimum_mean_oos_return:
            reasons.add("MEAN_OOS_RETURN_BELOW_THRESHOLD")
        if mean_excess < self.policy.minimum_mean_excess_return:
            reasons.add("MEAN_EXCESS_RETURN_BELOW_THRESHOLD")
        if worst_drawdown > self.policy.maximum_drawdown_fraction:
            reasons.add("MAX_DRAWDOWN_EXCEEDED")
        if self.policy.require_trade_in_each_window and any(
            window.trades == 0 for window in windows
        ):
            reasons.add("NO_TRADE_WINDOW")

        return StrategyQualification(
            qualified=not reasons,
            reasons=tuple(sorted(reasons)),
            windows=tuple(windows),
            mean_oos_return=mean_return,
            mean_excess_return=mean_excess,
            worst_drawdown_fraction=worst_drawdown,
            total_trades=total_trades,
        )
