from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_full_period_5m_postgres import BybitFullPeriod5mStoredCoverage
from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
)
from tools.research_bybit_full_period_fixed_strategy import (
    run_full_period_fixed_strategy_research,
)

_NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
_LAUNCH = _NOW - timedelta(days=120)


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


class _IncompleteStore:
    def __init__(self) -> None:
        self.load_called = False

    def coverage_state(self, symbols):
        return BybitFullPeriod5mStoredCoverage(
            completed_by_symbol={symbol: () for symbol in symbols},
            unavailable_retry_after_by_symbol={symbol: {} for symbol in symbols},
        )

    def load_bars(self, *, symbols, start_at=None, end_at=None):
        self.load_called = True
        raise AssertionError("incomplete coverage must fail before bar loading")


def test_full_period_research_fails_before_replay_when_archive_coverage_is_incomplete(
    tmp_path,
) -> None:
    store = _IncompleteStore()
    output = tmp_path / "full-period"

    with pytest.raises(RuntimeError, match="incomplete archive-day coverage"):
        run_full_period_fixed_strategy_research(
            store,
            output_dir=output,
            observed_at=_NOW,
            bybit_site="eu",
            universe_client=_Universe(),
        )

    assert store.load_called is False
    assert output.exists() is False
