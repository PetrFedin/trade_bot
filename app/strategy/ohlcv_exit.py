from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.position_management import (
    ExitReason,
    PositionManagementPolicy,
    TakeProfitMode,
    profit_protection_stop,
)


class IntrabarExitReason(StrEnum):
    HARD_STOP = "HARD_STOP"
    BREAK_EVEN_STOP = "BREAK_EVEN_STOP"
    PROFIT_PROTECTION = "PROFIT_PROTECTION"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"


@dataclass(frozen=True)
class IntrabarPositionState:
    peak_completed_price: Decimal
    trough_completed_price: Decimal | None = None

    def validate(self) -> None:
        if not self.peak_completed_price.is_finite() or self.peak_completed_price <= 0:
            raise ValueError("peak_completed_price must be positive and finite")
        if self.trough_completed_price is not None and (
            not self.trough_completed_price.is_finite()
            or self.trough_completed_price <= 0
        ):
            raise ValueError("trough_completed_price must be positive and finite")


@dataclass(frozen=True)
class IntrabarExitDecision:
    exit_now: bool
    reason: IntrabarExitReason | None
    exit_price_before_costs: Decimal | None
    hard_stop_price: Decimal
    take_profit_price: Decimal
    trailing_stop_price: Decimal | None
    profit_protection_stop_price: Decimal | None
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

    Trailing and profit-protection eligibility use only extrema from *completed prior
    bars*. The current bar high/low update MFE/MAE tracking only when the position
    survives the bar. Fixed take-profit preserves legacy behavior. In profit-runner
    mode, touching the take-profit threshold does not retroactively arm a same-bar stop;
    the completed bar peak may tighten protection only from the next bar onward.
    """

    policy.validate()
    state.validate()
    bar.validate()
    if not average_cost.is_finite() or average_cost <= 0:
        raise ValueError("average_cost must be positive and finite")

    hard_stop = average_cost * (Decimal("1") - policy.stop_loss_fraction)
    take_profit = average_cost * (Decimal("1") + policy.take_profit_fraction)
    fixed_take_profit = policy.take_profit_mode is TakeProfitMode.FIXED_EXIT
    trailing_active = state.peak_completed_price >= average_cost * (
        Decimal("1") + policy.trailing_activation_fraction
    )
    trailing_stop = (
        state.peak_completed_price * (Decimal("1") - policy.trailing_stop_fraction)
        if trailing_active
        else None
    )
    protected_profit_stop, protected_profit_reason = profit_protection_stop(
        average_cost=average_cost,
        peak_reference_price=state.peak_completed_price,
        policy=policy,
    )

    protective_candidates: list[tuple[Decimal, IntrabarExitReason]] = [
        (hard_stop, IntrabarExitReason.HARD_STOP)
    ]
    if protected_profit_stop is not None and protected_profit_reason is not None:
        protective_candidates.append(
            (
                protected_profit_stop,
                {
                    ExitReason.BREAK_EVEN_STOP: IntrabarExitReason.BREAK_EVEN_STOP,
                    ExitReason.PROFIT_PROTECTION: IntrabarExitReason.PROFIT_PROTECTION,
                }[protected_profit_reason],
            )
        )
    if trailing_stop is not None:
        protective_candidates.append(
            (trailing_stop, IntrabarExitReason.TRAILING_STOP)
        )
    protective_price, protective_reason = max(
        protective_candidates, key=lambda item: item[0]
    )

    if bar.open <= protective_price:
        return IntrabarExitDecision(
            exit_now=True,
            reason=protective_reason,
            exit_price_before_costs=bar.open,
            hard_stop_price=hard_stop,
            take_profit_price=take_profit,
            trailing_stop_price=trailing_stop,
            profit_protection_stop_price=protected_profit_stop,
            ambiguous_bar=False,
            gap_through_protective_stop=bar.open < protective_price,
            state=state,
        )

    if fixed_take_profit and bar.open >= take_profit:
        return IntrabarExitDecision(
            exit_now=True,
            reason=IntrabarExitReason.TAKE_PROFIT,
            exit_price_before_costs=take_profit,
            hard_stop_price=hard_stop,
            take_profit_price=take_profit,
            trailing_stop_price=trailing_stop,
            profit_protection_stop_price=protected_profit_stop,
            ambiguous_bar=False,
            gap_through_protective_stop=False,
            state=state,
        )

    protective_hit = bar.low <= protective_price
    take_profit_hit = fixed_take_profit and bar.high >= take_profit
    if protective_hit:
        return IntrabarExitDecision(
            exit_now=True,
            reason=protective_reason,
            exit_price_before_costs=protective_price,
            hard_stop_price=hard_stop,
            take_profit_price=take_profit,
            trailing_stop_price=trailing_stop,
            profit_protection_stop_price=protected_profit_stop,
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
            profit_protection_stop_price=protected_profit_stop,
            ambiguous_bar=False,
            gap_through_protective_stop=False,
            state=state,
        )

    prior_trough = (
        state.peak_completed_price
        if state.trough_completed_price is None
        else state.trough_completed_price
    )
    return IntrabarExitDecision(
        exit_now=False,
        reason=None,
        exit_price_before_costs=None,
        hard_stop_price=hard_stop,
        take_profit_price=take_profit,
        trailing_stop_price=trailing_stop,
        profit_protection_stop_price=protected_profit_stop,
        ambiguous_bar=False,
        gap_through_protective_stop=False,
        state=IntrabarPositionState(
            peak_completed_price=max(state.peak_completed_price, bar.high),
            trough_completed_price=min(prior_trough, bar.low),
        ),
    )
