from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_selection import CrossSectionalSelector

START = datetime(2026, 1, 2, tzinfo=UTC)


def symbol_bars(symbol: str, closes: list[str]) -> list[OhlcvBar]:
    return [
        OhlcvBar(
            symbol=symbol,
            timestamp=START + timedelta(days=index),
            open=Decimal(close),
            high=Decimal(close) + Decimal("0.5"),
            low=Decimal(close) - Decimal("0.5"),
            close=Decimal(close),
            volume=1000 + index,
            trade_count=100 + index,
            vwap=Decimal(close),
        )
        for index, close in enumerate(closes)
    ]


def test_selector_ranks_only_eligible_symbols_by_signal_quality() -> None:
    aapl = symbol_bars(
        "AAPL", ["100", "101", "102", "103", "104", "105", "106", "107"]
    )
    msft = symbol_bars(
        "MSFT", ["100", "100.5", "101", "101.5", "102", "103", "104", "105"]
    )
    nvda = symbol_bars(
        "NVDA", ["100", "101", "100", "101", "100", "101", "100", "120"]
    )
    result = CrossSectionalSelector(top_k=2).select([*aapl, *msft, *nvda])
    assert result.selected_symbols == ("AAPL", "MSFT")
    by_symbol = {candidate.symbol: candidate for candidate in result.candidates}
    assert by_symbol["AAPL"].rank == 1
    assert by_symbol["MSFT"].rank == 2
    assert by_symbol["NVDA"].rank is None
    assert by_symbol["NVDA"].eligible is False
    assert "REALIZED_VOLATILITY_ABOVE_LIMIT" in by_symbol["NVDA"].rejection_reasons


def test_selector_fails_when_latest_decision_times_are_not_synchronized() -> None:
    aapl = symbol_bars(
        "AAPL", ["100", "101", "102", "103", "104", "105", "106", "107"]
    )
    msft = symbol_bars(
        "MSFT", ["100", "101", "102", "103", "104", "105", "106", "107"]
    )
    msft[-1] = OhlcvBar(
        symbol="MSFT",
        timestamp=msft[-1].timestamp + timedelta(days=1),
        open=msft[-1].open,
        high=msft[-1].high,
        low=msft[-1].low,
        close=msft[-1].close,
        volume=msft[-1].volume,
        trade_count=msft[-1].trade_count,
        vwap=msft[-1].vwap,
    )
    with pytest.raises(ValueError, match="same decision timestamp"):
        CrossSectionalSelector(top_k=1).select([*aapl, *msft])
