from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class ExitReason(StrEnum):
    SIGNAL_EXIT = "SIGNAL_EXIT"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_STOP = "TIME_STOP"


@dataclass(frozen=True)
class PositionManagementPolicy:
    stop_loss_fraction: Decimal = Decimal("0.02")
    take_profit_fraction: Decimal = Decimal("0.04")
    trailing_activation_fraction: Decimal = Decimal("0.02")
    trailing_stop_fraction: Decimal = Decimal("0.015")
    maximum_holding_bars: int = 10

    def validate(self) -> None:
        for name, value in (
            ("stop_loss_fraction", self.stop_loss_fraction),
            ("take_profit_fraction", self.take_profit_fraction),
            ("trailing_activation_fraction", self.trailing_activation_fraction),
            ("trailing_stop_fraction", self.trailing_stop_fraction),
        ):
            if not value.is_finite() or value < 0 or value >= 1:
                raise ValueError(f"{name} must be finite and within [0, 1)")
        if self.stop_loss_fraction <= 0:
            raise ValueError("stop_loss_fraction must be positive")
        if self.take_profit_fraction <= 0:
            raise ValueError("take_profit_fraction must be positive")
        if self.trailing_stop_fraction <= 0:
            raise ValueError("trailing_stop_fraction must be positive")
        if self.maximum_holding_bars < 1:
            raise ValueError("maximum_holding_bars must be positive")


@dataclass(frozen=True)
class PositionTrackingState:
    entry_execution_index: int
    peak_reference_price: Decimal

    def validate(self) -> None:
        if self.entry_execution_index < 0:
            raise ValueError("entry_execution_index must be non-negative")
        if not self.peak_reference_price.is_finite() or self.peak_reference_price <= 0:
            raise ValueError("peak_reference_price must be positive and finite")


@dataclass(frozen=True)
class PositionExitDecision:
    exit_now: bool
    reason: ExitReason | None
    profit_fraction: Decimal
    drawdown_from_peak_fraction: Decimal
    peak_reference_price: Decimal


def evaluate_position_exit(
    *,
    average_cost: Decimal,
    reference_price: Decimal,
    state: PositionTrackingState,
    current_execution_index: int,
    policy: PositionManagementPolicy,
) -> PositionExitDecision:
    policy.validate()
    state.validate()
    if not average_cost.is_finite() or average_cost <= 0:
        raise ValueError("average_cost must be positive and finite")
    if not reference_price.is_finite() or reference_price <= 0:
        raise ValueError("reference_price must be positive and finite")
    if current_execution_index < state.entry_execution_index:
        raise ValueError("current_execution_index precedes position entry")

    peak = max(state.peak_reference_price, reference_price)
    profit_fraction = (reference_price - average_cost) / average_cost
    drawdown_from_peak = (peak - reference_price) / peak
    peak_profit_fraction = (peak - average_cost) / average_cost
    holding_bars = current_execution_index - state.entry_execution_index

    reason: ExitReason | None = None
    if profit_fraction <= -policy.stop_loss_fraction:
        reason = ExitReason.STOP_LOSS
    elif profit_fraction >= policy.take_profit_fraction:
        reason = ExitReason.TAKE_PROFIT
    elif (
        peak_profit_fraction >= policy.trailing_activation_fraction
        and drawdown_from_peak >= policy.trailing_stop_fraction
    ):
        reason = ExitReason.TRAILING_STOP
    elif holding_bars >= policy.maximum_holding_bars:
        reason = ExitReason.TIME_STOP

    return PositionExitDecision(
        exit_now=reason is not None,
        reason=reason,
        profit_fraction=profit_fraction,
        drawdown_from_peak_fraction=drawdown_from_peak,
        peak_reference_price=peak,
    )
