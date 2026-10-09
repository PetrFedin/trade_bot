"""Conservative portfolio-level funding bounds from immutable settlement snapshots.

Daily OHLCV identifies the bar in which an intrabar exit occurred, but not the exact
second inside that bar. Funding therefore cannot be represented as one falsely precise
number. This module produces timing bounds:

- definite settlements are strictly inside the known holding interval;
- settlements exactly at entry/exit boundaries are ambiguous;
- an intrabar exit may have occurred anywhere inside its daily bar, so every settlement
  before the next bar is treated as timing-ambiguous;
- negative rates are credits to a long and are included when they tighten the lower
  bound; positive ambiguous rates tighten the upper bound.

Funding notional is approximated with constant entry execution notional because the
daily-bar evidence does not contain the 8h/variable-interval mark price. The result is
therefore a conservative timing bound on an entry-notional approximation, not a claim
of exchange-statement-exact funding cash flow.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.strategy.cross_sectional_portfolio import (
    PortfolioExitReason,
    PortfolioTrade,
)

_INTRABAR_REASONS = {
    PortfolioExitReason.INTRABAR_HARD_STOP,
    PortfolioExitReason.INTRABAR_TAKE_PROFIT,
    PortfolioExitReason.INTRABAR_TRAILING_STOP,
}


@dataclass(frozen=True)
class FundingSettlement:
    timestamp: datetime
    rate: Decimal


@dataclass(frozen=True)
class FundingOpenPosition:
    symbol: str
    entry_time: datetime
    valuation_end: datetime
    quantity: Decimal
    entry_execution_price: Decimal


@dataclass(frozen=True)
class PortfolioFundingBounds:
    lower_cost: Decimal
    upper_cost: Decimal
    definite_settlement_count: int
    ambiguous_settlement_count: int
    trade_count: int
    open_position_count: int
    notional_model: str = "CONSTANT_ENTRY_EXECUTION_NOTIONAL"


def load_funding_schedule(path: Path) -> tuple[FundingSettlement, ...]:
    rows: list[FundingSettlement] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                FundingSettlement(
                    timestamp=datetime.fromisoformat(
                        row["timestamp"].replace("Z", "+00:00")
                    ),
                    rate=Decimal(row["funding_rate"]),
                )
            )
    rows.sort(key=lambda item: item.timestamp)
    return tuple(rows)


def _rate_bounds_for_interval(
    *,
    entry_time: datetime,
    exit_time: datetime,
    schedule: tuple[FundingSettlement, ...],
    intrabar_exit: bool,
) -> tuple[Decimal, Decimal, int, int]:
    definite: list[Decimal] = []
    ambiguous: list[Decimal] = []
    possible_end = exit_time + timedelta(days=1) if intrabar_exit else exit_time

    for settlement in schedule:
        timestamp = settlement.timestamp
        if timestamp < entry_time:
            continue
        if intrabar_exit:
            if timestamp >= possible_end:
                break
        elif timestamp > possible_end:
            break

        if entry_time < timestamp < exit_time:
            definite.append(settlement.rate)
        else:
            ambiguous.append(settlement.rate)

    definite_net = sum(definite, Decimal("0"))
    lower_rate = definite_net + sum(
        (min(rate, Decimal("0")) for rate in ambiguous),
        Decimal("0"),
    )
    upper_rate = definite_net + sum(
        (max(rate, Decimal("0")) for rate in ambiguous),
        Decimal("0"),
    )
    return lower_rate, upper_rate, len(definite), len(ambiguous)


def _rate_bounds_for_trade(
    trade: PortfolioTrade,
    schedule: tuple[FundingSettlement, ...],
) -> tuple[Decimal, Decimal, int, int]:
    return _rate_bounds_for_interval(
        entry_time=trade.entry_time,
        exit_time=trade.exit_time,
        schedule=schedule,
        intrabar_exit=trade.exit_reason in _INTRABAR_REASONS,
    )


def funding_bounds_for_trades(
    trades: tuple[PortfolioTrade, ...],
    schedules: dict[str, tuple[FundingSettlement, ...]],
    *,
    open_positions: tuple[FundingOpenPosition, ...] = (),
) -> PortfolioFundingBounds:
    lower = Decimal("0")
    upper = Decimal("0")
    definite_count = 0
    ambiguous_count = 0

    for trade in trades:
        if trade.symbol not in schedules:
            raise ValueError(f"funding schedule missing for {trade.symbol}")
        lower_rate, upper_rate, definite, ambiguous = _rate_bounds_for_trade(
            trade,
            schedules[trade.symbol],
        )
        notional = trade.quantity * trade.entry_execution_price
        lower += max(Decimal("0"), lower_rate * notional)
        upper += max(Decimal("0"), upper_rate * notional)
        definite_count += definite
        ambiguous_count += ambiguous

    for position in open_positions:
        if position.symbol not in schedules:
            raise ValueError(f"funding schedule missing for {position.symbol}")
        lower_rate, upper_rate, definite, ambiguous = _rate_bounds_for_interval(
            entry_time=position.entry_time,
            exit_time=position.valuation_end,
            schedule=schedules[position.symbol],
            intrabar_exit=False,
        )
        notional = position.quantity * position.entry_execution_price
        lower += max(Decimal("0"), lower_rate * notional)
        upper += max(Decimal("0"), upper_rate * notional)
        definite_count += definite
        ambiguous_count += ambiguous

    if lower > upper:
        raise ValueError("funding lower bound cannot exceed upper bound")

    return PortfolioFundingBounds(
        lower_cost=lower,
        upper_cost=upper,
        definite_settlement_count=definite_count,
        ambiguous_settlement_count=ambiguous_count,
        trade_count=len(trades),
        open_position_count=len(open_positions),
    )
