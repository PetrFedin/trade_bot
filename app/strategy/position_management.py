from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class ExitReason(StrEnum):
    SIGNAL_EXIT = "SIGNAL_EXIT"
    STOP_LOSS = "STOP_LOSS"
    BREAK_EVEN_STOP = "BREAK_EVEN_STOP"
    PROFIT_PROTECTION = "PROFIT_PROTECTION"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    TIME_STOP = "TIME_STOP"


class TakeProfitMode(StrEnum):
    FIXED_EXIT = "FIXED_EXIT"
    PROFIT_RUNNER = "PROFIT_RUNNER"


@dataclass(frozen=True)
class PositionManagementPolicy:
    stop_loss_fraction: Decimal = Decimal("0.02")
    take_profit_fraction: Decimal = Decimal("0.04")
    trailing_activation_fraction: Decimal = Decimal("0.02")
    trailing_stop_fraction: Decimal = Decimal("0.015")
    maximum_holding_bars: int = 10
    break_even_activation_fraction: Decimal | None = None
    break_even_buffer_fraction: Decimal = Decimal("0")
    profit_protection_activation_fraction: Decimal | None = None
    maximum_profit_giveback_fraction: Decimal = Decimal("0.50")
    take_profit_mode: TakeProfitMode = TakeProfitMode.FIXED_EXIT

    def validate(self) -> None:
        for name, value in (
            ("stop_loss_fraction", self.stop_loss_fraction),
            ("take_profit_fraction", self.take_profit_fraction),
            ("trailing_activation_fraction", self.trailing_activation_fraction),
            ("trailing_stop_fraction", self.trailing_stop_fraction),
            ("break_even_buffer_fraction", self.break_even_buffer_fraction),
            ("maximum_profit_giveback_fraction", self.maximum_profit_giveback_fraction),
        ):
            if not value.is_finite() or value < 0 or value >= 1:
                raise ValueError(f"{name} must be finite and within [0, 1)")
        for name, value in (
            ("break_even_activation_fraction", self.break_even_activation_fraction),
            (
                "profit_protection_activation_fraction",
                self.profit_protection_activation_fraction,
            ),
        ):
            if value is not None and (
                not value.is_finite() or value <= 0 or value >= 1
            ):
                raise ValueError(f"{name} must be finite and within (0, 1)")
        if not isinstance(self.take_profit_mode, TakeProfitMode):
            raise ValueError("take_profit_mode must be a TakeProfitMode")
        if self.stop_loss_fraction <= 0:
            raise ValueError("stop_loss_fraction must be positive")
        if self.take_profit_fraction <= 0:
            raise ValueError("take_profit_fraction must be positive")
        if self.trailing_stop_fraction <= 0:
            raise ValueError("trailing_stop_fraction must be positive")
        if self.maximum_holding_bars < 1:
            raise ValueError("maximum_holding_bars must be positive")
        if self.break_even_activation_fraction is not None:
            if self.break_even_activation_fraction >= self.take_profit_fraction:
                raise ValueError("break-even activation must precede take-profit")
            if self.break_even_buffer_fraction >= self.break_even_activation_fraction:
                raise ValueError("break-even buffer must be below its activation threshold")
        elif self.break_even_buffer_fraction != 0:
            raise ValueError("break-even buffer requires break-even activation")
        if self.profit_protection_activation_fraction is not None:
            if self.profit_protection_activation_fraction >= self.take_profit_fraction:
                raise ValueError("profit-protection activation must precede take-profit")
            if self.maximum_profit_giveback_fraction <= 0:
                raise ValueError("maximum_profit_giveback_fraction must be positive")


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
    maximum_favorable_excursion_fraction: Decimal
    protected_stop_price: Decimal


def profit_protection_stop(
    *,
    average_cost: Decimal,
    peak_reference_price: Decimal,
    policy: PositionManagementPolicy,
) -> tuple[Decimal | None, ExitReason | None]:
    """Return the strongest profit-preserving stop implied by a completed peak.

    The helper is intentionally separate from trailing-stop logic so an execution
    engine can combine both and choose the highest currently active protective level.
    """

    policy.validate()
    if not average_cost.is_finite() or average_cost <= 0:
        raise ValueError("average_cost must be positive and finite")
    if not peak_reference_price.is_finite() or peak_reference_price <= 0:
        raise ValueError("peak_reference_price must be positive and finite")

    peak_profit_fraction = (peak_reference_price - average_cost) / average_cost
    candidates: list[tuple[Decimal, ExitReason]] = []
    if (
        policy.break_even_activation_fraction is not None
        and peak_profit_fraction >= policy.break_even_activation_fraction
    ):
        candidates.append(
            (
                average_cost * (Decimal("1") + policy.break_even_buffer_fraction),
                ExitReason.BREAK_EVEN_STOP,
            )
        )
    if (
        policy.profit_protection_activation_fraction is not None
        and peak_profit_fraction >= policy.profit_protection_activation_fraction
    ):
        retained_profit_fraction = peak_profit_fraction * (
            Decimal("1") - policy.maximum_profit_giveback_fraction
        )
        candidates.append(
            (
                average_cost * (Decimal("1") + retained_profit_fraction),
                ExitReason.PROFIT_PROTECTION,
            )
        )
    if not candidates:
        return None, None
    return max(candidates, key=lambda item: item[0])


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

    protective_candidates: list[tuple[Decimal, ExitReason]] = [
        (
            average_cost * (Decimal("1") - policy.stop_loss_fraction),
            ExitReason.STOP_LOSS,
        )
    ]
    protected_profit_price, protected_profit_reason = profit_protection_stop(
        average_cost=average_cost,
        peak_reference_price=peak,
        policy=policy,
    )
    if protected_profit_price is not None and protected_profit_reason is not None:
        protective_candidates.append((protected_profit_price, protected_profit_reason))
    if peak_profit_fraction >= policy.trailing_activation_fraction:
        protective_candidates.append(
            (
                peak * (Decimal("1") - policy.trailing_stop_fraction),
                ExitReason.TRAILING_STOP,
            )
        )
    protected_stop_price, protected_stop_reason = max(
        protective_candidates, key=lambda item: item[0]
    )

    reason: ExitReason | None = None
    if (
        policy.take_profit_mode is TakeProfitMode.FIXED_EXIT
        and profit_fraction >= policy.take_profit_fraction
    ):
        reason = ExitReason.TAKE_PROFIT
    elif reference_price <= protected_stop_price:
        reason = protected_stop_reason
    elif holding_bars >= policy.maximum_holding_bars:
        reason = ExitReason.TIME_STOP

    return PositionExitDecision(
        exit_now=reason is not None,
        reason=reason,
        profit_fraction=profit_fraction,
        drawdown_from_peak_fraction=drawdown_from_peak,
        peak_reference_price=peak,
        maximum_favorable_excursion_fraction=max(peak_profit_fraction, Decimal("0")),
        protected_stop_price=protected_stop_price,
    )
