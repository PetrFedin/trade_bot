from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.position_management import PositionManagementPolicy


class IntrabarExitReason(StrEnum):
    HARD_STOP = "HARD_STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"


@dataclass(frozen=True)
class IntrabarPositionState:
    peak_completed_price: Decimal

    def validate(self) -> None:
        if not self.peak_completed_price.is_finite() or self.peak_completed_price <= 0:
            raise ValueError("peak_completed_price must be positive and finite")


@dataclass(frozen=True)
class IntrabarExitDecision:
    exit_now: bool
    reason: IntrabarExitReason | None
    exit_price_before_costs: Decimal | None
    hard_stop_price: Decimal
    take_profit_price: Decimal
    trailing_stop_price: Decimal | None
    ambiguous_bar: bool
    gap_through_protective_stop: bool
    state: IntrabarPositionState


def evaluate_long_intrabar_exit(
    *,
    average_cost: Decimal,
    bar: OhlcvBar,
    state: IntrabarPositionState,
    policy: PositionManagementPolicy,
) -> IntrabarExitDecision:
    """Evaluate one long-position OHLCV bar without assuming high/low ordering.

    Trailing-stop eligibility and level use only the peak from *completed prior bars*.
    The current bar high updates the peak only when the position survives the bar. If
    both a protective stop and take-profit are reachable inside the same bar, the
    protective exit is chosen to avoid optimistic OHLC path reconstruction.
    """

    policy.validate()
    state.validate()
    bar.validate()
    if not average_cost.is_finite() or average_cost <= 0:
        raise ValueError("average_cost must be positive and finite")

    hard_stop = average_cost * (Decimal("1") - policy.stop_loss_fraction)
    take_profit = average_cost * (Decimal("1") + policy.take_profit_fraction)
    trailing_active = state.peak_completed_price >= average_cost * (
        Decimal("1") + policy.trailing_activation_fraction
    )
    trailing_stop = (
        state.peak_completed_price * (Decimal("1") - policy.trailing_stop_fraction)
        if trailing_active
        else None
    )
    protective_price = hard_stop
    protective_reason = IntrabarExitReason.HARD_STOP
    if trailing_stop is not None and trailing_stop > protective_price:
        protective_price = trailing_stop
        protective_reason = IntrabarExitReason.TRAILING_STOP

    if bar.open <= protective_price:
        return IntrabarExitDecision(
            exit_now=True,
            reason=protective_reason,
            exit_price_before_costs=bar.open,
            hard_stop_price=hard_stop,
            take_profit_price=take_profit,
            trailing_stop_price=trailing_stop,
            ambiguous_bar=False,
            gap_through_protective_stop=bar.open < protective_price,
            state=state,
        )

    if bar.open >= take_profit:
        return IntrabarExitDecision(
            exit_now=True,
            reason=IntrabarExitReason.TAKE_PROFIT,
            exit_price_before_costs=take_profit,
            hard_stop_price=hard_stop,
            take_profit_price=take_profit,
            trailing_stop_price=trailing_stop,
            ambiguous_bar=False,
            gap_through_protective_stop=False,
            state=state,
        )

    protective_hit = bar.low <= protective_price
    take_profit_hit = bar.high >= take_profit
    if protective_hit:
        return IntrabarExitDecision(
            exit_now=True,
            reason=protective_reason,
            exit_price_before_costs=protective_price,
            hard_stop_price=hard_stop,
            take_profit_price=take_profit,
            trailing_stop_price=trailing_stop,
            ambiguous_bar=take_profit_hit,
            gap_through_protective_stop=False,
            state=state,
        )
    if take_profit_hit:
        return IntrabarExitDecision(
            exit_now=True,
            reason=IntrabarExitReason.TAKE_PROFIT,
            exit_price_before_costs=take_profit,
            hard_stop_price=hard_stop,
            take_profit_price=take_profit,
            trailing_stop_price=trailing_stop,
            ambiguous_bar=False,
            gap_through_protective_stop=False,
            state=state,
        )

    return IntrabarExitDecision(
        exit_now=False,
        reason=None,
        exit_price_before_costs=None,
        hard_stop_price=hard_stop,
        take_profit_price=take_profit,
        trailing_stop_price=trailing_stop,
        ambiguous_bar=False,
        gap_through_protective_stop=False,
        state=IntrabarPositionState(
            peak_completed_price=max(state.peak_completed_price, bar.high)
        ),
    )
