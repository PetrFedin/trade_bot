from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.marketdata.ohlcv import MultiSymbolOhlcvDataset, OhlcvBar


def bar(symbol: str, *, close: str = "101") -> OhlcvBar:
    return OhlcvBar(
        symbol=symbol,
        timestamp=datetime(2026, 1, 2, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=1000,
        trade_count=100,
        vwap=Decimal("100.5"),
    )


def test_ohlcv_bar_rejects_invalid_price_envelope() -> None:
    invalid = OhlcvBar(
        symbol="AAPL",
        timestamp=datetime(2026, 1, 2, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=1,
        trade_count=1,
    )
    with pytest.raises(ValueError, match="high"):
        invalid.validate()


def test_multisymbol_dataset_requires_unique_sorted_bars() -> None:
    aapl = bar("AAPL")
    msft = bar("MSFT")
    dataset = MultiSymbolOhlcvDataset(
        provider="alpaca",
        feed="iex",
        timeframe="1Day",
        adjustment="all",
        bars=(aapl, msft),
        request_ids=("req-1",),
    )
    dataset.validate(minimum_symbols=2)
    assert dataset.symbols == ("AAPL", "MSFT")
    assert dataset.counts_by_symbol() == {"AAPL": 1, "MSFT": 1}


def test_multisymbol_dataset_rejects_duplicate_symbol_timestamp() -> None:
    aapl = bar("AAPL")
    dataset = MultiSymbolOhlcvDataset(
        provider="alpaca",
        feed="iex",
        timeframe="1Day",
        adjustment="all",
        bars=(aapl, aapl),
    )
    with pytest.raises(ValueError, match="duplicate"):
        dataset.validate()
