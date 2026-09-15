from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.continuity import SQLiteOperationalRepairBarStore
from app.marketdata.operational import OperationalBar, SQLiteOperationalMarketDataStore

BASE = datetime(2026, 9, 16, 10, 0, tzinfo=UTC)
OBSERVED = BASE + timedelta(minutes=5, seconds=2)
STRATEGY = "bybit-demo-momentum-v1"


def candle(*, source_timestamp: datetime) -> OperationalBar:
    close_time = BASE + timedelta(minutes=5)
    return OperationalBar(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        open_time=BASE,
        close_time=close_time,
        source_timestamp=source_timestamp,
        received_at=OBSERVED,
        source_event_id=(
            f"kline.5.BTCUSDT:{int(BASE.timestamp() * 1000)}:"
            f"{int(close_time.timestamp() * 1000) - 1}"
        ),
        is_final=True,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
        revision=0,
    )


def test_sqlite_repair_accepts_matching_economics_after_live_envelope_wins(tmp_path) -> None:
    path = tmp_path / "marketdata.sqlite"
    marketdata = SQLiteOperationalMarketDataStore(path)
    repair = SQLiteOperationalRepairBarStore(path)
    close_time = BASE + timedelta(minutes=5)
    live = candle(source_timestamp=close_time - timedelta(seconds=2))
    repaired = candle(source_timestamp=close_time - timedelta(milliseconds=1))
    assert live.bar_id == repaired.bar_id
    assert live.content_hash != repaired.content_hash

    ticket = marketdata.record_finalized_for_strategy(
        live,
        strategy_id=STRATEGY,
        recorded_at=OBSERVED,
    )
    assert not repair.record_without_decision(
        repaired,
        recorded_at=OBSERVED + timedelta(seconds=1),
    )

    assert marketdata.conflict_count() == 0
    assert marketdata.pending_decisions(strategy_id=STRATEGY) == (ticket,)
