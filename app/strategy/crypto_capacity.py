from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

_BPS = Decimal("10000")


@dataclass(frozen=True)
class CryptoCapacityPolicy:
    maximum_execution_cost_fraction: Decimal = Decimal("0.02")

    def validate(self) -> None:
        if not Decimal("0") < self.maximum_execution_cost_fraction < Decimal("1"):
            raise ValueError("crypto capacity cost budget must be within (0, 1)")


@dataclass(frozen=True)
class CryptoCapacityEstimate:
    opening_equity_usdt: Decimal
    notional_usdt: Decimal
    target_net_profit_usd: Decimal
    estimated_round_trip_cost_usdt: Decimal
    execution_cost_budget_usdt: Decimal
    maximum_full_cost_round_trips: int
    minimum_gross_profit_usdt: Decimal
    minimum_price_move_fraction: Decimal
    requested_trades_per_day: int | None
    requested_frequency_within_cost_budget: bool | None
    theoretical_daily_net_target_usdt: Decimal | None
    live_promotion_allowed: bool = False


def estimate_crypto_trade_capacity(
    *,
    opening_equity_usdt: Decimal,
    notional_to_equity: Decimal,
    target_net_profit_usd: Decimal,
    taker_fee_rate: Decimal,
    slippage_bps_per_fill: Decimal,
    requested_trades_per_day: int | None = None,
    policy: CryptoCapacityPolicy | None = None,
) -> CryptoCapacityEstimate:
    """Estimate turnover capacity before alpha assumptions.

    This is a cost/risk diagnostic, not a profitability forecast. It deliberately assumes
    every requested round trip pays taker fee and adverse slippage on both sides.
    """

    active_policy = CryptoCapacityPolicy() if policy is None else policy
    active_policy.validate()
    positives = (
        opening_equity_usdt,
        notional_to_equity,
        target_net_profit_usd,
        taker_fee_rate,
    )
    if any(not value.is_finite() or value <= 0 for value in positives):
        raise ValueError("crypto capacity inputs must be positive and finite")
    if not slippage_bps_per_fill.is_finite() or slippage_bps_per_fill < 0:
        raise ValueError("crypto capacity slippage cannot be negative")
    if requested_trades_per_day is not None and requested_trades_per_day < 0:
        raise ValueError("requested crypto trades per day cannot be negative")

    notional = opening_equity_usdt * notional_to_equity
    per_fill_cost_rate = taker_fee_rate + slippage_bps_per_fill / _BPS
    round_trip_cost = notional * per_fill_cost_rate * Decimal("2")
    cost_budget = opening_equity_usdt * active_policy.maximum_execution_cost_fraction
    maximum_round_trips = int(
        (cost_budget / round_trip_cost).to_integral_value(rounding=ROUND_FLOOR)
    )
    gross_profit_required = target_net_profit_usd + round_trip_cost
    minimum_move_fraction = gross_profit_required / notional
    within_budget = None
    theoretical_daily_target = None
    if requested_trades_per_day is not None:
        within_budget = requested_trades_per_day <= maximum_round_trips
        theoretical_daily_target = target_net_profit_usd * Decimal(requested_trades_per_day)

    return CryptoCapacityEstimate(
        opening_equity_usdt=opening_equity_usdt,
        notional_usdt=notional,
        target_net_profit_usd=target_net_profit_usd,
        estimated_round_trip_cost_usdt=round_trip_cost,
        execution_cost_budget_usdt=cost_budget,
        maximum_full_cost_round_trips=maximum_round_trips,
        minimum_gross_profit_usdt=gross_profit_required,
        minimum_price_move_fraction=minimum_move_fraction,
        requested_trades_per_day=requested_trades_per_day,
        requested_frequency_within_cost_budget=within_budget,
        theoretical_daily_net_target_usdt=theoretical_daily_target,
    )
