from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


@dataclass(frozen=True)
class OhlcvBar:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trade_count: int
    vwap: Decimal | None = None

    def validate(self) -> None:
        if not _SYMBOL_RE.fullmatch(self.symbol):
            raise ValueError("OHLCV symbol must be normalized uppercase market symbol")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("OHLCV timestamp must be timezone-aware")
        prices = {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
        }
        for name, value in prices.items():
            if not value.is_finite() or value <= 0:
                raise ValueError(f"OHLCV {name} must be positive and finite")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("OHLCV high is below another price field")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("OHLCV low is above another price field")
        if self.volume < 0:
            raise ValueError("OHLCV volume must be non-negative")
        if self.trade_count < 0:
            raise ValueError("OHLCV trade_count must be non-negative")
        if self.vwap is not None and (not self.vwap.is_finite() or self.vwap <= 0):
            raise ValueError("OHLCV vwap must be positive and finite when supplied")


@dataclass(frozen=True)
class MultiSymbolOhlcvDataset:
    provider: str
    feed: str
    timeframe: str
    adjustment: str
    bars: tuple[OhlcvBar, ...]
    request_ids: tuple[str, ...] = ()

    def validate(self, *, minimum_symbols: int = 1) -> None:
        if self.provider != "alpaca":
            raise ValueError("unsupported OHLCV provider")
        if not self.feed:
            raise ValueError("OHLCV feed is required")
        if not self.timeframe:
            raise ValueError("OHLCV timeframe is required")
        if not self.adjustment:
            raise ValueError("OHLCV adjustment is required")
        seen: set[tuple[str, datetime]] = set()
        symbols: set[str] = set()
        previous: tuple[str, datetime] | None = None
        for bar in self.bars:
            bar.validate()
            key = (bar.symbol, bar.timestamp)
            if key in seen:
                raise ValueError("duplicate OHLCV symbol/timestamp")
            if previous is not None and key < previous:
                raise ValueError("OHLCV bars must be sorted by symbol then timestamp")
            seen.add(key)
            symbols.add(bar.symbol)
            previous = key
        if len(symbols) < minimum_symbols:
            raise ValueError("insufficient distinct symbols in OHLCV dataset")
        if len(set(self.request_ids)) != len(self.request_ids):
            raise ValueError("duplicate provider request IDs")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({bar.symbol for bar in self.bars}))

    def counts_by_symbol(self) -> dict[str, int]:
        counts = Counter(bar.symbol for bar in self.bars)
        return dict(sorted(counts.items()))

    def bars_for(self, symbol: str) -> tuple[OhlcvBar, ...]:
        return tuple(bar for bar in self.bars if bar.symbol == symbol)


def normalize_bars(bars: Iterable[OhlcvBar]) -> tuple[OhlcvBar, ...]:
    ordered = tuple(sorted(bars, key=lambda bar: (bar.symbol, bar.timestamp)))
    for bar in ordered:
        bar.validate()
    return ordered
