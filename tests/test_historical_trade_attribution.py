from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.strategy.cross_sectional_portfolio import PortfolioExitReason, PortfolioTrade
from tools.historical_trade_attribution import attribute_closed_trades


def trade(symbol="BTCUSDT", reason=PortfolioExitReason.INTRABAR_HARD_STOP, holding=1):
    return PortfolioTrade(
        symbol=symbol, entry_time=datetime(2026, 1, 1, tzinfo=UTC),
        exit_time=datetime(2026, 1, 2, tzinfo=UTC),
        entry_reference_price=Decimal("100"), exit_reference_price=Decimal("102"),
        entry_execution_price=Decimal("100.05"), exit_execution_price=Decimal("101.95"),
        entry_fee=Decimal("0.08"), exit_fee=Decimal("0.08"),
        slippage_cost=Decimal("0.10"), gross_pnl_before_costs=Decimal("2"),
        quantity=Decimal("1"), net_pnl=Decimal("1.74"),
        holding_bars=holding, exit_reason=reason,
    )


def test_exact_pnl_identity_and_attribution() -> None:
    report = attribute_closed_trades((
        trade(), trade("ETHUSDT", PortfolioExitReason.TIME_STOP, 4),
    ))
    assert report["total"]["trades"] == 2
    assert report["total"]["gross_pnl"] == "4"
    assert Decimal(report["total"]["net_pnl_before_funding"]) == Decimal("3.48")
    assert report["by_holding_bars"]["0-1"]["trades"] == 1
    assert report["by_holding_bars"]["4+"]["trades"] == 1
    assert report["by_exit_reason"]["TIME_STOP"]["trades"] == 1
    assert report["by_symbol"]["BTCUSDT"]["trades"] == 1


def test_inconsistent_trade_costs_are_rejected() -> None:
    from dataclasses import replace
    with pytest.raises(ValueError, match="inconsistent"):
        attribute_closed_trades((replace(trade(), net_pnl=Decimal("2")),))


def test_empty_trades_still_report_zero_not_fake_profit() -> None:
    report = attribute_closed_trades(())
    assert report["total"]["trades"] == 0
    assert report["total"]["net_pnl_before_funding"] == "0"
