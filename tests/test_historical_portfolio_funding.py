from datetime import UTC, datetime
from decimal import Decimal

from app.strategy.cross_sectional_portfolio import (
    PortfolioExitReason,
    PortfolioTrade,
)
from tools.historical_portfolio_funding import (
    FundingOpenPosition,
    FundingSettlement,
    funding_bounds_for_trades,
)


def _trade(reason: PortfolioExitReason) -> PortfolioTrade:
    return PortfolioTrade(
        symbol="BTCUSDT",
        entry_time=datetime(2026, 1, 1, tzinfo=UTC),
        exit_time=datetime(2026, 1, 2, tzinfo=UTC),
        entry_reference_price=Decimal("100"),
        exit_reference_price=Decimal("102"),
        entry_execution_price=Decimal("100"),
        exit_execution_price=Decimal("102"),
        entry_fee=Decimal("0"),
        exit_fee=Decimal("0"),
        slippage_cost=Decimal("0"),
        gross_pnl_before_costs=Decimal("2"),
        quantity=Decimal("1"),
        net_pnl=Decimal("2"),
        holding_bars=1,
        exit_reason=reason,
    )


def _schedule() -> tuple[FundingSettlement, ...]:
    return (
        FundingSettlement(datetime(2026, 1, 1, 0, tzinfo=UTC), Decimal("0.001")),
        FundingSettlement(datetime(2026, 1, 1, 8, tzinfo=UTC), Decimal("0.001")),
        FundingSettlement(datetime(2026, 1, 1, 16, tzinfo=UTC), Decimal("-0.0005")),
        FundingSettlement(datetime(2026, 1, 2, 0, tzinfo=UTC), Decimal("0.002")),
        FundingSettlement(datetime(2026, 1, 2, 8, tzinfo=UTC), Decimal("-0.001")),
        FundingSettlement(datetime(2026, 1, 2, 16, tzinfo=UTC), Decimal("0.003")),
        FundingSettlement(datetime(2026, 1, 3, 0, tzinfo=UTC), Decimal("0.010")),
    )


def test_intrabar_exit_produces_conservative_timing_bounds() -> None:
    bounds = funding_bounds_for_trades(
        (_trade(PortfolioExitReason.INTRABAR_HARD_STOP),),
        {"BTCUSDT": _schedule()},
    )

    assert bounds.lower_cost == Decimal("0")
    assert bounds.upper_cost == Decimal("0.6500")
    assert bounds.definite_settlement_count == 2
    assert bounds.ambiguous_settlement_count == 4


def test_open_exit_has_narrower_boundary_uncertainty() -> None:
    bounds = funding_bounds_for_trades(
        (_trade(PortfolioExitReason.SELECTION_EXIT),),
        {"BTCUSDT": _schedule()},
    )

    assert bounds.lower_cost == Decimal("0.0500")
    assert bounds.upper_cost == Decimal("0.3500")
    assert bounds.definite_settlement_count == 2
    assert bounds.ambiguous_settlement_count == 2


def test_final_open_position_is_included_through_valuation_close() -> None:
    open_position = FundingOpenPosition(
        symbol="BTCUSDT",
        entry_time=datetime(2026, 1, 1, 0, tzinfo=UTC),
        valuation_end=datetime(2026, 1, 2, 0, tzinfo=UTC),
        quantity=Decimal("1"),
        entry_execution_price=Decimal("100"),
    )
    bounds = funding_bounds_for_trades(
        (),
        {"BTCUSDT": _schedule()},
        open_positions=(open_position,),
    )

    assert bounds.lower_cost == Decimal("0.0500")
    assert bounds.upper_cost == Decimal("0.3500")
    assert bounds.trade_count == 0
    assert bounds.open_position_count == 1
