from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.correlation_diversification import (
    CorrelationDiversificationPolicy,
    DiversifiedCrossSectionalSelector,
    pairwise_return_correlation,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector

START = datetime(2026, 1, 2, tzinfo=UTC)


def symbol_bars(symbol: str, closes: list[str]) -> list[OhlcvBar]:
    return [
        OhlcvBar(
            symbol=symbol,
            timestamp=START + timedelta(days=index),
            open=Decimal(close),
            high=Decimal(close) + Decimal("0.3"),
            low=Decimal(close) - Decimal("0.3"),
            close=Decimal(close),
            volume=1000 + index,
            trade_count=100 + index,
            vwap=Decimal(close),
        )
        for index, close in enumerate(closes)
    ]


def universe() -> tuple[list[OhlcvBar], list[OhlcvBar], list[OhlcvBar]]:
    aapl = symbol_bars(
        "AAPL",
        ["100", "102", "104", "106", "108", "110", "112", "114", "116", "118", "120", "122"],
    )
    msft = symbol_bars(
        "MSFT",
        ["100", "102", "104", "106", "108", "110", "112", "114", "116", "118", "120", "122"],
    )
    nvda = symbol_bars(
        "NVDA",
        ["100", "101.5", "101", "102.5", "102", "103.5", "103", "104.5", "104", "105.5", "105", "106.5"],
    )
    return aapl, msft, nvda


def test_identical_return_paths_have_unit_correlation() -> None:
    aapl, msft, _ = universe()
    correlation = pairwise_return_correlation(
        tuple(aapl),
        tuple(msft),
        lookback_bars=6,
        minimum_return_observations=5,
    )
    assert correlation == Decimal("1")


def test_diversification_replaces_redundant_second_rank_with_next_candidate() -> None:
    aapl, msft, nvda = universe()
    bars = [*aapl, *msft, *nvda]
    base = CrossSectionalSelector(top_k=2)
    assert base.select(bars).selected_symbols == ("AAPL", "MSFT")

    diversified = DiversifiedCrossSectionalSelector(
        base_selector=base,
        policy=CorrelationDiversificationPolicy(
            lookback_bars=6,
            minimum_return_observations=5,
            maximum_pairwise_correlation=Decimal("0.80"),
        ),
    ).select_with_trace(bars)

    assert diversified.selection.selected_symbols == ("AAPL", "NVDA")
    assert len(diversified.blocks) == 1
    block = diversified.blocks[0]
    assert block.symbol == "MSFT"
    assert block.selected_symbol == "AAPL"
    assert block.correlation == Decimal("1")


def test_diversification_fails_closed_when_pairwise_history_is_insufficient() -> None:
    short_left = symbol_bars("AAPL", ["100", "101", "102"])
    short_right = symbol_bars("MSFT", ["100", "101", "102"])
    with pytest.raises(ValueError, match="insufficient common history"):
        pairwise_return_correlation(
            tuple(short_left),
            tuple(short_right),
            lookback_bars=6,
            minimum_return_observations=5,
        )
