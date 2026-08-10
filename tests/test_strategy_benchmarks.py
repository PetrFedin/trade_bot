from __future__ import annotations

from decimal import Decimal

import pytest

from app.strategy.backtest import BacktestConfig
from app.strategy.benchmarks import (
    CAPITAL_MATCHED_BUY_HOLD_V1,
    evaluate_benchmarks,
)


def config(*, opening_cash: str = "10000") -> BacktestConfig:
    return BacktestConfig(
        opening_cash=Decimal(opening_cash),
        fee_per_fill=Decimal("0.50"),
        slippage_bps=Decimal("5"),
    )


def test_capital_matched_buy_hold_uses_strategy_notional_and_costs() -> None:
    result = evaluate_benchmarks(
        first_price=Decimal("100"),
        last_price=Decimal("110"),
        target_quantity=Decimal("1"),
        config=config(),
    )
    assert result.mode == CAPITAL_MATCHED_BUY_HOLD_V1
    assert result.cash_return == Decimal("0")
    assert result.asset_return == Decimal("0.10")
    assert result.capital_matched_buy_hold_return == Decimal("0.000945")


def test_capital_matched_return_scales_with_target_quantity_but_asset_return_does_not() -> None:
    one_share = evaluate_benchmarks(
        first_price=Decimal("100"),
        last_price=Decimal("110"),
        target_quantity=Decimal("1"),
        config=config(),
    )
    two_shares = evaluate_benchmarks(
        first_price=Decimal("100"),
        last_price=Decimal("110"),
        target_quantity=Decimal("2"),
        config=config(),
    )
    assert one_share.asset_return == two_shares.asset_return == Decimal("0.10")
    assert two_shares.capital_matched_buy_hold_return == Decimal("0.00194")
    assert (
        two_shares.capital_matched_buy_hold_return
        > one_share.capital_matched_buy_hold_return
    )


def test_capital_matched_benchmark_rejects_unfunded_notional() -> None:
    with pytest.raises(ValueError, match="BENCHMARK_INSUFFICIENT_CASH"):
        evaluate_benchmarks(
            first_price=Decimal("100"),
            last_price=Decimal("110"),
            target_quantity=Decimal("2"),
            config=config(opening_cash="100"),
        )
