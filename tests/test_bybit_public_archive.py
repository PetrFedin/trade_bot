import gzip
import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.marketdata.bybit_public_archive import (
    BybitPublicTradeArchiveClient,
    _validated_archive_path,
    archive_url,
    completed_archive_dates,
)


def _archive_bytes(symbol: str) -> bytes:
    text = "\n".join(
        [
            "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional",
            f"300.900,{symbol},Buy,2,103,PlusTick,id3,0,0,0",
            f"1.500,{symbol},Buy,1,100,PlusTick,id1,0,0,0",
            f"299.100,{symbol},Sell,3,101,MinusTick,id2,0,0,0",
            f"301.200,{symbol},Buy,4,102,PlusTick,id4,0,0,0",
            "",
        ]
    )
    return gzip.compress(text.encode("utf-8"))


def test_archive_trades_aggregate_to_chronological_five_minute_bars() -> None:
    def opener(url: str) -> io.BytesIO:
        symbol = url.split("/")[-2]
        return io.BytesIO(_archive_bytes(symbol))

    client = BybitPublicTradeArchiveClient(opener=opener)
    result = client.fetch_klines(
        symbols=("BTCUSDT", "ETHUSDT"),
        dates=(date(2026, 8, 11),),
        interval_minutes=5,
    )

    assert result.trade_rows_by_symbol == {"BTCUSDT": 4, "ETHUSDT": 4}
    assert result.klines.counts_by_symbol() == {"BTCUSDT": 2, "ETHUSDT": 2}
    btc = [bar for bar in result.klines.bars if bar.symbol == "BTCUSDT"]
    first = btc[0]
    assert first.start_time == datetime(1970, 1, 1, tzinfo=UTC)
    assert first.open == Decimal("100")
    assert first.high == Decimal("101")
    assert first.low == Decimal("100")
    assert first.close == Decimal("101")
    assert first.volume == Decimal("4")
    assert first.turnover == Decimal("403")
    second = btc[1]
    assert second.start_time == datetime(1970, 1, 1, 0, 5, tzinfo=UTC)
    assert second.open == Decimal("103")
    assert second.close == Decimal("102")
    assert second.high == Decimal("103")
    assert second.low == Decimal("102")


def test_completed_archive_dates_exclude_current_utc_day() -> None:
    now = datetime(2026, 8, 12, 23, 59, tzinfo=UTC)

    dates = completed_archive_dates(now=now, lookback_days=3)

    assert dates == (date(2026, 8, 9), date(2026, 8, 10), date(2026, 8, 11))


def test_archive_url_is_fixed_to_official_public_bybit_history_surface() -> None:
    url = archive_url("BTCUSDT", date(2026, 8, 11))
    assert url == "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2026-08-11.csv.gz"
    assert _validated_archive_path(url) == "/trading/BTCUSDT/BTCUSDT2026-08-11.csv.gz"


@pytest.mark.parametrize(
    "url",
    [
        "http://public.bybit.com/trading/BTCUSDT/BTCUSDT2026-08-11.csv.gz",
        "https://evil.example/trading/BTCUSDT/BTCUSDT2026-08-11.csv.gz",
        "https://user@public.bybit.com/trading/BTCUSDT/BTCUSDT2026-08-11.csv.gz",
        "https://public.bybit.com:444/trading/BTCUSDT/BTCUSDT2026-08-11.csv.gz",
        "https://public.bybit.com/trading/BTCUSDT/BTCUSDT2026-08-11.csv.gz?redirect=evil",
        "https://public.bybit.com/other/BTCUSDT/BTCUSDT2026-08-11.csv.gz",
    ],
)
def test_archive_transport_rejects_non_allowlisted_endpoints(url: str) -> None:
    with pytest.raises(ValueError):
        _validated_archive_path(url)


def test_archive_symbol_cannot_inject_path_components() -> None:
    with pytest.raises(ValueError):
        archive_url("BTCUSDT/../../evil", date(2026, 8, 11))