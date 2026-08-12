from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_selection import (
    CrossSectionalSelection,
    SelectionCandidate,
)
from app.strategy.entry_quality import (
    EntryQualityBlockReason,
    EntryQualityFilteredSelector,
    EntryQualityPolicy,
)

NOW = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)


def candidate(symbol: str, rank: int) -> SelectionCandidate:
    return SelectionCandidate(
        rank=rank,
        symbol=symbol,
        eligible=True,
        rejection_reasons=(),
        momentum_return=Decimal("0.05"),
        trend_strength=Decimal("0.03"),
        realized_volatility=Decimal("0.01"),
        quality_score=Decimal("0.07"),
        reference_price=Decimal("100"),
    )


class FakeBaseSelector:
    top_k = 2
    signal_config = object()

    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = symbols

    def select(self, bars: tuple[OhlcvBar, ...]) -> CrossSectionalSelection:
        del bars
        candidates = tuple(
            candidate(symbol, rank)
            for rank, symbol in enumerate(self.symbols, start=1)
        )
        return CrossSectionalSelection(
            decision_time=NOW,
            selected_symbols=self.symbols[: self.top_k],
            candidates=candidates,
        )


def bars(symbol: str, closes: list[str], *, volume: int = 1_000_000):
    result = []
    for index, close_text in enumerate(closes):
        close = Decimal(close_text)
        result.append(
            OhlcvBar(
                symbol=symbol,
                timestamp=NOW - timedelta(days=len(closes) - index),
                open=close,
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=volume,
                trade_count=1_000,
            )
        )
    return result


def test_late_spike_is_rejected_and_next_smooth_candidate_is_promoted() -> None:
    selector = EntryQualityFilteredSelector(
        base_selector=FakeBaseSelector(("MSFT", "AAPL", "NVDA")),
        policy=EntryQualityPolicy(
            lookback_bars=8,
            minimum_trend_efficiency=Decimal("0.35"),
            maximum_price_extension_fraction=Decimal("0.04"),
            maximum_single_bar_return_fraction=Decimal("0.05"),
        ),
    )
    universe = (
        bars(
            "MSFT",
            ["100", "100.5", "101", "101.5", "102", "102.5", "103", "112"],
        )
        + bars("AAPL", ["100", "101", "102", "103", "104", "105", "106", "107"])
        + bars("NVDA", ["80", "81", "82", "83", "84", "85", "86", "87"])
    )

    filtered, trace = selector.select_with_trace(universe)

    assert trace.base_selected_symbols == ("MSFT", "AAPL")
    assert filtered.selected_symbols == ("AAPL", "NVDA")
    msft = trace.evaluations[0]
    assert msft.symbol == "MSFT"
    assert msft.passed is False
    assert EntryQualityBlockReason.PRICE_EXTENSION_ABOVE_MAXIMUM in msft.block_reasons
    assert EntryQualityBlockReason.SINGLE_BAR_RETURN_ABOVE_MAXIMUM in msft.block_reasons


def test_choppy_path_is_rejected_even_when_base_rank_is_high() -> None:
    selector = EntryQualityFilteredSelector(
        base_selector=FakeBaseSelector(("AAPL", "MSFT")),
        policy=EntryQualityPolicy(
            lookback_bars=8,
            minimum_trend_efficiency=Decimal("0.35"),
            maximum_price_extension_fraction=Decimal("0.10"),
            maximum_single_bar_return_fraction=Decimal("0.10"),
        ),
    )
    universe = (
        bars("AAPL", ["100", "102", "99", "101", "98", "102", "99", "100.5"])
        + bars("MSFT", ["90", "91", "92", "93", "94", "95", "96", "97"])
    )

    filtered, trace = selector.select_with_trace(universe)

    assert filtered.selected_symbols == ("MSFT",)
    aapl = trace.evaluations[0]
    assert aapl.metrics.trend_efficiency is not None
    assert aapl.metrics.trend_efficiency < Decimal("0.35")
    assert aapl.block_reasons == (
        EntryQualityBlockReason.TREND_EFFICIENCY_BELOW_MINIMUM,
    )


def test_optional_dollar_volume_floor_blocks_thin_candidate() -> None:
    selector = EntryQualityFilteredSelector(
        base_selector=FakeBaseSelector(("AAPL", "MSFT")),
        policy=EntryQualityPolicy(
            lookback_bars=8,
            minimum_trend_efficiency=Decimal("0.30"),
            maximum_price_extension_fraction=Decimal("0.10"),
            maximum_single_bar_return_fraction=Decimal("0.10"),
            minimum_average_dollar_volume=Decimal("1000000"),
        ),
    )
    universe = (
        bars(
            "AAPL",
            ["100", "101", "102", "103", "104", "105", "106", "107"],
            volume=100,
        )
        + bars("MSFT", ["90", "91", "92", "93", "94", "95", "96", "97"])
    )

    filtered, trace = selector.select_with_trace(universe)

    assert filtered.selected_symbols == ("MSFT",)
    aapl = trace.evaluations[0]
    assert aapl.block_reasons == (
        EntryQualityBlockReason.AVERAGE_DOLLAR_VOLUME_BELOW_MINIMUM,
    )


def test_insufficient_entry_quality_history_fails_closed() -> None:
    selector = EntryQualityFilteredSelector(
        base_selector=FakeBaseSelector(("AAPL",)),
        policy=EntryQualityPolicy(lookback_bars=8),
    )

    filtered, trace = selector.select_with_trace(
        bars("AAPL", ["100", "101", "102", "103", "104", "105", "106"])
    )

    assert filtered.selected_symbols == ()
    assert trace.evaluations[0].block_reasons == (
        EntryQualityBlockReason.INSUFFICIENT_HISTORY,
    )
