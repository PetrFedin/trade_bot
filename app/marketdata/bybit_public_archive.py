from __future__ import annotations

import csv
import gzip
import io
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import BinaryIO
from urllib.error import HTTPError
from urllib.request import urlopen

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar

_PUBLIC_ARCHIVE_BASE = "https://public.bybit.com/trading"


class BybitArchiveUnavailableError(RuntimeError):
    pass


@dataclass
class _MutableBar:
    first_trade_time: Decimal
    last_trade_time: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal


@dataclass(frozen=True)
class BybitArchiveAcquisition:
    klines: BybitKlineAcquisition
    files_by_symbol: dict[str, tuple[str, ...]]
    trade_rows_by_symbol: dict[str, int]

    def validate(self, *, requested_symbols: tuple[str, ...], minimum_bars: int) -> None:
        self.klines.validate(
            requested_symbols=requested_symbols,
            minimum_bars=minimum_bars,
        )
        expected = set(requested_symbols)
        if set(self.files_by_symbol) != expected:
            raise ValueError("Bybit archive file map does not match requested symbols")
        if set(self.trade_rows_by_symbol) != expected:
            raise ValueError("Bybit archive trade-row map does not match requested symbols")
        if any(value <= 0 for value in self.trade_rows_by_symbol.values()):
            raise ValueError("Bybit archive contains a symbol with no trades")


ArchiveOpener = Callable[[str], BinaryIO]


class BybitPublicTradeArchiveClient:
    """Aggregate official Bybit public perpetual trade archives into OHLCV bars.

    The archive path is a fixed Bybit-owned historical-data surface, separate from the
    geo-restricted V5 REST API. Files are streamed and aggregated; raw third-party market
    data is not committed to the repository.
    """

    def __init__(self, *, opener: ArchiveOpener | None = None) -> None:
        self._opener = _open_archive if opener is None else opener

    def fetch_klines(
        self,
        *,
        symbols: tuple[str, ...],
        dates: Sequence[date],
        interval_minutes: int = 5,
    ) -> BybitArchiveAcquisition:
        _validate_request(symbols, dates, interval_minutes)
        all_bars: list[BybitKlineBar] = []
        files_by_symbol: dict[str, tuple[str, ...]] = {}
        rows_by_symbol: dict[str, int] = {}
        for symbol in symbols:
            symbol_bars: dict[int, _MutableBar] = {}
            urls: list[str] = []
            row_count = 0
            for archive_date in dates:
                url = archive_url(symbol, archive_date)
                urls.append(url)
                row_count += self._consume_file(
                    url,
                    symbol=symbol,
                    interval_minutes=interval_minutes,
                    bars=symbol_bars,
                )
            files_by_symbol[symbol] = tuple(urls)
            rows_by_symbol[symbol] = row_count
            all_bars.extend(_freeze_bars(symbol, symbol_bars))
        acquisition = BybitKlineAcquisition(
            bars=tuple(sorted(all_bars, key=lambda item: (item.symbol, item.start_time))),
            pages_by_symbol={symbol: len(dates) for symbol in symbols},
        )
        result = BybitArchiveAcquisition(acquisition, files_by_symbol, rows_by_symbol)
        result.validate(requested_symbols=symbols, minimum_bars=1)
        return result

    def _consume_file(
        self,
        url: str,
        *,
        symbol: str,
        interval_minutes: int,
        bars: dict[int, _MutableBar],
    ) -> int:
        try:
            with closing(self._opener(url)) as raw:
                with gzip.GzipFile(fileobj=raw, mode="rb") as compressed:
                    with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
                        reader = csv.DictReader(text)
                        _validate_header(reader.fieldnames)
                        count = 0
                        for row in reader:
                            _consume_trade_row(
                                row,
                                expected_symbol=symbol,
                                interval_minutes=interval_minutes,
                                bars=bars,
                            )
                            count += 1
                        if count == 0:
                            raise BybitArchiveUnavailableError(
                                f"Bybit archive file contained no trades:{symbol}"
                            )
                        return count
        except HTTPError as exc:
            raise BybitArchiveUnavailableError(
                f"Bybit archive download failed:{symbol}:HTTP_{exc.code}"
            ) from exc
        except (OSError, UnicodeError, csv.Error) as exc:
            raise BybitArchiveUnavailableError(
                f"Bybit archive decode failed:{symbol}"
            ) from exc


def completed_archive_dates(*, now: datetime, lookback_days: int) -> tuple[date, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("archive cutoff time must be timezone-aware")
    if lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    utc_today = now.astimezone(UTC).date()
    first = utc_today - timedelta(days=lookback_days)
    return tuple(first + timedelta(days=offset) for offset in range(lookback_days))


def archive_url(symbol: str, archive_date: date) -> str:
    normalized = symbol.strip().upper()
    if normalized != symbol or not symbol.endswith("USDT") or not symbol.isalnum():
        raise ValueError("Bybit archive symbol must be normalized USDT symbol")
    stamp = archive_date.isoformat()
    return f"{_PUBLIC_ARCHIVE_BASE}/{symbol}/{symbol}{stamp}.csv.gz"


def _consume_trade_row(
    row: Mapping[str, str | None],
    *,
    expected_symbol: str,
    interval_minutes: int,
    bars: dict[int, _MutableBar],
) -> None:
    row_symbol = row.get("symbol")
    if row_symbol not in (None, "", expected_symbol):
        raise ValueError("Bybit archive trade symbol mismatch")
    try:
        trade_time = Decimal(str(row["timestamp"]))
        price = Decimal(str(row["price"]))
        size = Decimal(str(row["size"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid Bybit archive trade row") from exc
    if not trade_time.is_finite() or trade_time < 0:
        raise ValueError("Bybit archive timestamp must be non-negative and finite")
    if not price.is_finite() or price <= 0:
        raise ValueError("Bybit archive price must be positive and finite")
    if not size.is_finite() or size < 0:
        raise ValueError("Bybit archive size must be non-negative and finite")
    interval_seconds = interval_minutes * 60
    bucket = int(
        (trade_time / Decimal(interval_seconds)).to_integral_value(rounding=ROUND_FLOOR)
    ) * interval_seconds
    turnover = price * size
    current = bars.get(bucket)
    if current is None:
        bars[bucket] = _MutableBar(
            first_trade_time=trade_time,
            last_trade_time=trade_time,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=size,
            turnover=turnover,
        )
        return
    if trade_time < current.first_trade_time:
        current.first_trade_time = trade_time
        current.open = price
    if trade_time >= current.last_trade_time:
        current.last_trade_time = trade_time
        current.close = price
    current.high = max(current.high, price)
    current.low = min(current.low, price)
    current.volume += size
    current.turnover += turnover


def _freeze_bars(symbol: str, bars: Mapping[int, _MutableBar]) -> list[BybitKlineBar]:
    result: list[BybitKlineBar] = []
    for bucket, bar in sorted(bars.items()):
        frozen = BybitKlineBar(
            symbol=symbol,
            start_time=datetime.fromtimestamp(bucket, tz=UTC),
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            turnover=bar.turnover,
        )
        frozen.validate()
        result.append(frozen)
    return result


def _validate_request(
    symbols: tuple[str, ...],
    dates: Sequence[date],
    interval_minutes: int,
) -> None:
    if len(symbols) < 2:
        raise ValueError("Bybit archive replay requires at least two symbols")
    normalized = tuple(symbol.strip().upper() for symbol in symbols)
    if normalized != symbols or len(set(symbols)) != len(symbols):
        raise ValueError("Bybit archive symbols must be unique normalized uppercase")
    if not dates:
        raise ValueError("Bybit archive replay requires dates")
    if tuple(sorted(set(dates))) != tuple(dates):
        raise ValueError("Bybit archive dates must be unique and chronological")
    if interval_minutes <= 0:
        raise ValueError("Bybit archive interval must be positive")


def _validate_header(fieldnames: list[str] | None) -> None:
    if fieldnames is None:
        raise ValueError("Bybit archive CSV is missing a header")
    missing = {"timestamp", "price", "size"} - set(fieldnames)
    if missing:
        raise ValueError(f"Bybit archive CSV missing columns:{sorted(missing)}")


def _open_archive(url: str) -> BinaryIO:
    return urlopen(url, timeout=90)  # noqa: S310 - URL is constructed from a fixed Bybit host.
