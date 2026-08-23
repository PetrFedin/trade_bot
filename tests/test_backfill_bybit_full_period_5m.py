from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_full_period_5m_postgres import BybitFullPeriod5mStoredCoverage
from app.marketdata.bybit_public_archive import BybitArchiveAcquisition
from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
)
from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from tools.backfill_bybit_full_period_5m import run_full_period_5m_backfill_cycle

_NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
_LAUNCH = datetime(2026, 5, 1, tzinfo=UTC)


def _instrument(symbol: str) -> BybitResearchInstrument:
    return BybitResearchInstrument(
        symbol=symbol,
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
        symbol_type="innovation",
        launch_time_ms=int(_LAUNCH.timestamp() * 1000),
        delivery_time_ms=0,
        is_pre_listing=False,
    )


def _ticker(symbol: str, index: int) -> BybitResearchTicker:
    return BybitResearchTicker(
        symbol=symbol,
        last_price=Decimal("100") + index,
        bid_price=Decimal("99.95") + index,
        ask_price=Decimal("100.05") + index,
        turnover_24h_usdt=Decimal("500000000") - index * Decimal("10000000"),
        volume_24h=Decimal("1000000"),
        open_interest=Decimal("500000"),
        open_interest_value_usdt=Decimal("100000000") - index * Decimal("1000000"),
        funding_rate=Decimal("0.0001"),
        price_24h_fraction=Decimal("0.02"),
    )


class _Universe:
    def __init__(self) -> None:
        self.symbols = tuple(f"C{index:02d}USDT" for index in range(10))

    def fetch_instruments(self):
        return tuple(_instrument(symbol) for symbol in self.symbols)

    def fetch_tickers(self):
        return tuple(_ticker(symbol, index) for index, symbol in enumerate(self.symbols))


class _Store:
    def __init__(self) -> None:
        self.completed: dict[str, set[date]] = {}
        self.unavailable: dict[str, dict[date, datetime]] = {}

    def coverage_state(self, symbols):
        return BybitFullPeriod5mStoredCoverage(
            completed_by_symbol={
                symbol: tuple(sorted(self.completed.get(symbol, set()))) for symbol in symbols
            },
            unavailable_retry_after_by_symbol={
                symbol: dict(self.unavailable.get(symbol, {})) for symbol in symbols
            },
        )

    def persist_complete_day(
        self,
        *,
        symbol,
        archive_date,
        acquisition,
        observed_at,
    ):
        acquisition.validate(requested_symbols=(symbol,), minimum_bars=1)
        self.completed.setdefault(symbol, set()).add(archive_date)
        self.unavailable.setdefault(symbol, {}).pop(archive_date, None)
        return "a" * 64

    def persist_unavailable(
        self,
        *,
        symbol,
        archive_date,
        error_code,
        retry_after,
        observed_at,
    ):
        assert error_code
        assert retry_after > observed_at
        self.unavailable.setdefault(symbol, {})[archive_date] = retry_after
        return "b" * 64


class _Archive:
    def __init__(self) -> None:
        self.requests: list[tuple[str, date]] = []

    def fetch_klines(self, *, symbols, dates, interval_minutes=5):
        assert len(symbols) == 1
        assert len(dates) == 1
        assert interval_minutes == 5
        symbol = symbols[0]
        archive_date = dates[0]
        self.requests.append((symbol, archive_date))
        start = datetime.combine(archive_date, datetime.min.time(), tzinfo=UTC)
        bar = BybitKlineBar(
            symbol=symbol,
            start_time=start,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("10"),
            turnover=Decimal("1000"),
        )
        return BybitArchiveAcquisition(
            klines=BybitKlineAcquisition(
                bars=(bar,),
                pages_by_symbol={symbol: 1},
            ),
            files_by_symbol={symbol: (f"fake://{symbol}/{archive_date}",)},
            trade_rows_by_symbol={symbol: 1},
        )


def test_backfill_cycle_is_bounded_and_advances_oldest_missing_days() -> None:
    universe = _Universe()
    store = _Store()
    archive = _Archive()

    plan, summary = run_full_period_5m_backfill_cycle(
        store,
        observed_at=_NOW,
        bybit_site="eu",
        maximum_days_per_run=3,
        universe_client=universe,
        archive_client=archive,
    )

    assert summary.universe_host == "api.bybit.eu"
    assert len(summary.top10_symbols) == 10
    assert summary.attempted_day_count == 3
    assert summary.completed_this_run == 3
    assert summary.unavailable_this_run == 0
    assert plan.completed_day_count == 3
    assert plan.full_period_complete is False
    assert summary.full_period_claim_allowed is False
    assert summary.trade_actionable is False
    assert summary.bybit_live_order_routing_allowed is False
    assert len(archive.requests) == 3
    assert {value[1] for value in archive.requests} == {date(2026, 5, 1)}


def test_recent_unavailable_day_is_blocked_until_retry_after() -> None:
    universe = _Universe()
    store = _Store()
    first_symbol = sorted(universe.symbols)[0]
    store.unavailable[first_symbol] = {
        date(2026, 5, 1): _NOW + timedelta(hours=12)
    }
    archive = _Archive()

    plan, _summary = run_full_period_5m_backfill_cycle(
        store,
        observed_at=_NOW,
        bybit_site="eu",
        maximum_days_per_run=1,
        universe_client=universe,
        archive_client=archive,
    )

    first = next(item for item in plan.coverage if item.symbol == first_symbol)
    assert first.blocked_dates == (date(2026, 5, 1),)
    assert archive.requests[0] != (first_symbol, date(2026, 5, 1))
