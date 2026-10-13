"""Negative controls for optional, research-only per-coin exit parameters."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    PortfolioExitReason,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.position_management import PositionManagementPolicy
from tools.per_asset_exit_policy_matrix import EXIT_ARMS, SIX

D = Decimal


def _sample():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    prices = {
        "AAPL": ["100", "101", "102", "103", "104", "105",
                 "106", "108", "108", "109", "110"],
        "MSFT": ["100", "100.5", "101", "101.5", "102", "102.5",
                 "103", "104", "104.5", "105", "105.5"],
        "NVDA": ["107", "106", "105", "104", "103", "102",
                 "101", "100", "99", "98", "97"],
    }
    rows = []
    for symbol, closes in prices.items():
        for i, close in enumerate(closes):
            opening, high, low = D(close), D(close) + D("0.2"), D(close) - D("0.2")
            if symbol == "AAPL" and i == 8:
                opening, high, low = D("108"), D("108.2"), D("104")
            rows.append(OhlcvBar(
                symbol=symbol, timestamp=start + timedelta(days=i),
                open=opening, high=high, low=low, close=D(close),
                volume=1000, trade_count=100, vwap=D(close),
            ))
    return rows


def _portfolio(overrides=None):
    return CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=2),
        portfolio_policy=CrossSectionalPortfolioPolicy(
            opening_cash=D("10000"), fee_per_fill=D("0.50"),
            slippage_bps=D("5"),
            maximum_gross_exposure_fraction=D("0.60"),
            new_position_target_equity_fraction=D("0.29"),
        ),
        position_policy=PositionManagementPolicy(),
        position_policies_by_symbol=overrides,
    )


def test_default_and_empty_overrides_are_identical() -> None:
    left = _portfolio().run(_sample())
    right = _portfolio({}).run(_sample())
    assert left == right
    assert left.closed_trade_count == right.closed_trade_count


def test_specific_symbol_stop_changes_only_its_modelled_exit() -> None:
    shipped = _portfolio().run(_sample())
    widened = _portfolio({
        "AAPL": PositionManagementPolicy(stop_loss_fraction=D("0.04")),
    }).run(_sample())
    original = next(t for t in shipped.closed_trades if t.symbol == "AAPL")
    assert original.exit_reason is PortfolioExitReason.INTRABAR_HARD_STOP
    assert original.holding_bars == 0
    assert not any(
        t.symbol == "AAPL" and t.holding_bars == 0
        and t.exit_reason is PortfolioExitReason.INTRABAR_HARD_STOP
        for t in widened.closed_trades
    )
    assert widened.decision_trace[0].entered_symbols == (
        shipped.decision_trace[0].entered_symbols
    )
    assert "MSFT" not in widened.decision_trace[0].intrabar_exit_symbols


def test_unknown_symbol_and_malformed_policy_fail_closed() -> None:
    with pytest.raises(ValueError, match="unknown symbol"):
        _portfolio({"UNKNOWN": PositionManagementPolicy()}).run(_sample())
    with pytest.raises(ValueError, match="invalid symbol"):
        _portfolio({"": PositionManagementPolicy()})
    with pytest.raises(ValueError, match="validated policy"):
        _portfolio({"AAPL": None})


def test_external_mapping_mutation_does_not_change_frozen_backtest() -> None:
    initial = PositionManagementPolicy(stop_loss_fraction=D("0.04"))
    values = {"AAPL": initial}
    backtester = _portfolio(values)
    values["AAPL"] = PositionManagementPolicy(stop_loss_fraction=D("0.01"))
    assert backtester._position_policy_for("AAPL") is initial
    assert backtester._position_policy_for("MSFT") == PositionManagementPolicy()


def test_all_four_single_coordinate_arms_preserve_protection() -> None:
    assert len(SIX) == 6
    assert len(EXIT_ARMS) == 4
    for _, (field, value) in EXIT_ARMS.items():
        from dataclasses import replace

        p = replace(PositionManagementPolicy(), **{field: value})
        p.validate()
        assert p.stop_loss_fraction > 0
        assert p.take_profit_fraction > 0
        assert sum(
            getattr(p, f) != getattr(PositionManagementPolicy(), f)
            for f in ("stop_loss_fraction", "take_profit_fraction")
        ) == 1
