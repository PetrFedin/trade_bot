from __future__ import annotations

import gzip
import io
from datetime import UTC, date, datetime
from decimal import Decimal

from app.marketdata.bybit_public_archive import BybitPublicTradeArchiveClient


def test_public_archive_supports_one_symbol_day_for_incremental_backfill() -> None:
    rows = (
        b"timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional\n"
        b"1787443201,BTCUSDT,Buy,1,100,PlusTick,x,0,0,0\n"
        b"1787443301,BTCUSDT,Sell,2,101,PlusTick,y,0,0,0\n"
        b"1787443501,BTCUSDT,Buy,1,99,MinusTick,z,0,0,0\n"
    )
    compressed = gzip.compress(rows)
    opened: list[str] = []

    def opener(url: str):
        opened.append(url)
        return io.BytesIO(compressed)

    client = BybitPublicTradeArchiveClient(opener=opener)
    acquisition = client.fetch_klines(
        symbols=("BTCUSDT",),
        dates=(date(2026, 8, 23),),
        interval_minutes=5,
    )

    acquisition.validate(requested_symbols=("BTCUSDT",), minimum_bars=1)
    assert len(opened) == 1
    assert opened[0].endswith("/BTCUSDT/BTCUSDT2026-08-23.csv.gz")
    assert acquisition.trade_rows_by_symbol == {"BTCUSDT": 3}
    assert acquisition.klines.pages_by_symbol == {"BTCUSDT": 1}
    assert len(acquisition.klines.bars) == 2
    first = acquisition.klines.bars[0]
    assert first.symbol == "BTCUSDT"
    assert first.start_time == datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
    assert first.open == Decimal("100")
    assert first.high == Decimal("101")
    assert first.low == Decimal("100")
    assert first.close == Decimal("101")
    assert first.volume == Decimal("3")
    assert first.turnover == Decimal("302")
