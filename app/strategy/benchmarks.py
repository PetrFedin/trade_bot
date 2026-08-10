from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.strategy.backtest import BacktestConfig

CAPITAL_MATCHED_BUY_HOLD_V1 = "capital_matched_buy_hold_v1"


@dataclass(frozen=True)
class BenchmarkSuite:
    """Comparable OOS baselines for one strategy execution window.

    `asset_return` is the raw price move and is informational only. The acceptance
    baseline is `capital_matched_buy_hold_return`: it uses the strategy target quantity,
    opening cash, entry slippage and per-fill fee, then marks the position to the last
    OOS close without forcing an exit. `cash_return` is the zero-return cash control.
    """

    mode: str
    cash_return: Decimal
    asset_return: Decimal
    capital_matched_buy_hold_return: Decimal


def evaluate_benchmarks(
    *,
    first_price: Decimal,
    last_price: Decimal,
    target_quantity: Decimal,
    config: BacktestConfig,
) -> BenchmarkSuite:
    config.validate()
    for name, value in (("first_price", first_price), ("last_price", last_price)):
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be positive and finite")
    if not target_quantity.is_finite() or target_quantity <= 0:
        raise ValueError("target_quantity must be positive and finite")

    raw_asset_return = (last_price - first_price) / first_price
    slip = config.slippage_bps / Decimal("10000")
    entry_price = first_price * (Decimal("1") + slip)
    entry_cash_cost = target_quantity * entry_price + config.fee_per_fill
    if entry_cash_cost > config.opening_cash:
        raise ValueError("BENCHMARK_INSUFFICIENT_CASH")

    ending_equity = (
        config.opening_cash
        - entry_cash_cost
        + target_quantity * last_price
    )
    capital_matched_return = (
        ending_equity - config.opening_cash
    ) / config.opening_cash
    return BenchmarkSuite(
        mode=CAPITAL_MATCHED_BUY_HOLD_V1,
        cash_return=Decimal("0"),
        asset_return=raw_asset_return,
        capital_matched_buy_hold_return=capital_matched_return,
    )
