from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    PortfolioEntryBlockReason,
    PortfolioExitReason,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.position_sizing import RiskAwareSizingPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy

START = datetime(2026, 1, 2, tzinfo=UTC)


def series(
    symbol: str,
    closes: list[str],
    *,
    overrides: dict[int, tuple[str, str, str, str]] | None = None,
) -> list[OhlcvBar]:
    overrides = {} if overrides is None else overrides
    result: list[OhlcvBar] = []
    for index, close in enumerate(closes):
        if index in overrides:
            open_value, high_value, low_value, close_value = overrides[index]
        else:
            close_value = close
            open_value = close
            high_value = str(Decimal(close) + Decimal("0.2"))
            low_value = str(Decimal(close) - Decimal("0.2"))
        result.append(
            OhlcvBar(
                symbol=symbol,
                timestamp=START + timedelta(days=index),
                open=Decimal(open_value),
                high=Decimal(high_value),
                low=Decimal(low_value),
                close=Decimal(close_value),
                volume=1000 + index,
                trade_count=100 + index,
                vwap=Decimal(close_value),
            )
        )
    return result


def policy() -> CrossSectionalPortfolioPolicy:
    return CrossSectionalPortfolioPolicy(
        opening_cash=Decimal("10000"),
        fee_per_fill=Decimal("0.50"),
        slippage_bps=Decimal("5"),
        maximum_gross_exposure_fraction=Decimal("0.60"),
        new_position_target_equity_fraction=Decimal("0.29"),
    )


def stable_universe(*, aapl_stop_on_entry: bool = False) -> list[OhlcvBar]:
    aapl_overrides = (
        {8: ("108", "108.2", "104", "108")}
        if aapl_stop_on_entry
        else None
    )
    return [
        *series(
            "AAPL",
            ["100", "101", "102", "103", "104", "105", "106", "108", "108", "109", "110"],
            overrides=aapl_overrides,
        ),
        *series(
            "MSFT",
            ["100", "100.5", "101", "101.5", "102", "102.5", "103", "104", "104.5", "105", "105.5"],
        ),
        *series(
            "NVDA",
            ["107", "106", "105", "104", "103", "102", "101", "100", "99", "98", "97"],
        ),
    ]


def test_top_two_selection_becomes_bounded_portfolio_positions() -> None:
    result = CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=2),
        portfolio_policy=policy(),
        position_policy=PositionManagementPolicy(),
    ).run(stable_universe())

    first = result.decision_trace[0]
    assert first.selected_symbols == ("AAPL", "MSFT")
    assert first.entered_symbols == ("AAPL", "MSFT")
    assert first.concurrent_positions == 2
    assert result.maximum_concurrent_positions == 2
    assert result.selection_counts["AAPL"] > 0
    assert result.selection_counts["MSFT"] > 0
    assert "NVDA" not in result.selection_counts
    assert result.turnover_fraction >= Decimal("0.57")
    assert result.maximum_gross_exposure_fraction_observed < Decimal("0.62")
    assert result.one_bar_reentry_count == 0


def test_intrabar_stop_is_symbol_specific_and_reentry_waits_for_confirmation() -> None:
    result = CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=2),
        portfolio_policy=policy(),
        position_policy=PositionManagementPolicy(),
        reentry_policy=ReentryConfirmationPolicy(
            minimum_consecutive_eligible_bars=2
        ),
    ).run(stable_universe(aapl_stop_on_entry=True))

    first = result.decision_trace[0]
    assert set(first.entered_symbols) == {"AAPL", "MSFT"}
    assert first.intrabar_exit_symbols == ("AAPL",)
    aapl_trade = next(trade for trade in result.closed_trades if trade.symbol == "AAPL")
    assert aapl_trade.exit_reason is PortfolioExitReason.INTRABAR_HARD_STOP
    assert aapl_trade.net_pnl < 0

    second = result.decision_trace[1]
    assert second.selected_symbols == ("AAPL", "MSFT")
    assert "AAPL" not in second.entered_symbols
    assert (
        "AAPL",
        PortfolioEntryBlockReason.REENTRY_CONFIRMATION_PENDING,
    ) in second.blocked_entries

    third = result.decision_trace[2]
    assert "AAPL" in third.entered_symbols
    assert result.entry_block_counts["REENTRY_CONFIRMATION_PENDING"] == 1
    assert result.one_bar_reentry_count == 0
    assert result.final_quantities["MSFT"] > 0


def test_ranking_rotation_exits_old_symbol_before_entering_new_symbol() -> None:
    universe = [
        *series(
            "AAPL",
            ["100", "101", "102", "103", "104", "105", "106", "108", "106.5", "107"],
            overrides={8: ("108", "108.2", "106.4", "106.5")},
        ),
        *series(
            "MSFT",
            ["100", "100.5", "101", "101.5", "102", "102.5", "103", "104", "105.5", "106"],
        ),
        *series(
            "NVDA",
            ["100", "100.2", "100.4", "100.6", "100.8", "101", "101.2", "101.4", "102.8", "103.5"],
        ),
    ]
    result = CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=1),
        portfolio_policy=policy(),
        position_policy=PositionManagementPolicy(),
    ).run(universe)

    first = result.decision_trace[0]
    assert first.selected_symbols == ("AAPL",)
    assert first.entered_symbols == ("AAPL",)

    second = result.decision_trace[1]
    assert second.selected_symbols == ("MSFT",)
    assert second.open_exit_symbols == ("AAPL",)
    assert second.entered_symbols == ("MSFT",)
    trade = next(
        trade
        for trade in result.closed_trades
        if trade.symbol == "AAPL" and trade.exit_reason is PortfolioExitReason.SELECTION_EXIT
    )
    assert trade.exit_time == second.execution_time
    assert result.fill_count >= 3


def test_portfolio_profit_protection_preserves_confirmed_intrabar_gain() -> None:
    universe = [
        *series(
            "AAPL",
            ["100", "101", "102", "103", "104", "105", "106", "107", "110", "109.5", "109"],
            overrides={
                8: ("108", "110.5", "107.8", "110"),
                9: ("110", "110.2", "109", "109.5"),
            },
        ),
        *series(
            "MSFT",
            ["107", "106", "105", "104", "103", "102", "101", "100", "99", "98", "97"],
        ),
    ]
    result = CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=1),
        portfolio_policy=policy(),
        position_policy=PositionManagementPolicy(
            trailing_activation_fraction=Decimal("0.03"),
            break_even_activation_fraction=Decimal("0.01"),
            break_even_buffer_fraction=Decimal("0.001"),
            profit_protection_activation_fraction=Decimal("0.015"),
            maximum_profit_giveback_fraction=Decimal("0.50"),
        ),
    ).run(universe)

    trade = next(trade for trade in result.closed_trades if trade.symbol == "AAPL")
    assert trade.exit_reason is PortfolioExitReason.INTRABAR_PROFIT_PROTECTION
    assert trade.net_pnl > 0
    assert trade.maximum_favorable_excursion_fraction > 0
    assert trade.mfe_capture_ratio is not None
    assert trade.mfe_capture_ratio > 0
    assert result.positive_mfe_closed_profitable >= 1
    assert result.profit_preservation_rate is not None
    assert result.profit_preservation_rate > 0


def test_risk_aware_sizing_reduces_notional_for_high_volatility_candidate() -> None:
    universe = [
        *series(
            "AAPL",
            ["100", "99", "101", "100", "102", "101", "104", "108", "109"],
        ),
        *series(
            "MSFT",
            ["108", "107", "106", "105", "104", "103", "102", "101", "100"],
        ),
    ]
    result = CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=1),
        portfolio_policy=policy(),
        position_policy=PositionManagementPolicy(),
        sizing_policy=RiskAwareSizingPolicy(
            target_realized_volatility=Decimal("0.01")
        ),
    ).run(universe)

    first = result.decision_trace[0]
    assert first.selected_symbols == ("AAPL",)
    assert first.entered_symbols == ("AAPL",)
    assert Decimal("0") < first.closing_gross_exposure_fraction < Decimal("0.20")
    assert result.final_quantities["AAPL"] > 0
