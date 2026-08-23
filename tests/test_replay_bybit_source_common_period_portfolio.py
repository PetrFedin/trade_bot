from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_full_period_5m_postgres import BybitFullPeriod5mStoredCoverage
from app.marketdata.bybit_full_period_derivatives import DERIVATIVES_SOURCES
from app.marketdata.bybit_full_period_derivatives_postgres import (
    BybitFullPeriodDerivativesStoredCoverage,
)
from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
)
from tools import replay_bybit_source_common_period_portfolio as portfolio_tool

_OBSERVED = datetime(2026, 4, 10, 12, tzinfo=UTC)
_LAST_DAY = date(2026, 4, 9)


def _launch(index: int) -> datetime:
    return datetime(2026, 1, 3 if index == 9 else 1, tzinfo=UTC)


def _instrument(symbol: str, index: int) -> BybitResearchInstrument:
    return BybitResearchInstrument(
        symbol=symbol,
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
        symbol_type="innovation",
        launch_time_ms=int(_launch(index).timestamp() * 1000),
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
        open_interest=Decimal("1000000"),
        open_interest_value_usdt=Decimal("200000000") - index * Decimal("1000000"),
        funding_rate=Decimal("0.0001"),
        price_24h_fraction=Decimal("0.02"),
    )


def _dates(first: date) -> tuple[date, ...]:
    return tuple(
        first + timedelta(days=index)
        for index in range((_LAST_DAY - first).days + 1)
    )


class _Universe:
    def __init__(self) -> None:
        self.symbols = tuple(f"C{index:02d}USDT" for index in range(10))
        self.instruments = tuple(
            _instrument(symbol, index) for index, symbol in enumerate(self.symbols)
        )
        self.tickers = tuple(
            _ticker(symbol, index) for index, symbol in enumerate(self.symbols)
        )
        self.instrument_calls = 0
        self.ticker_calls = 0

    def fetch_instruments(self):
        self.instrument_calls += 1
        return self.instruments

    def fetch_tickers(self):
        self.ticker_calls += 1
        return self.tickers


class _PriceStore:
    def __init__(self, universe: _Universe) -> None:
        self.universe = universe
        self.load_args: dict[str, Any] | None = None

    def coverage_state(self, symbols):
        return BybitFullPeriod5mStoredCoverage(
            completed_by_symbol={
                symbol: _dates(_launch(index).date())
                for index, symbol in enumerate(self.universe.symbols)
                if symbol in symbols
            },
            unavailable_retry_after_by_symbol={symbol: {} for symbol in symbols},
        )

    def load_bars(self, *, symbols, start_at=None, end_at=None):
        self.load_args = {
            "symbols": tuple(symbols),
            "start_at": start_at,
            "end_at": end_at,
        }
        return ()


class _DerivativesStore:
    def __init__(self, universe: _Universe) -> None:
        self.universe = universe
        self.loads: list[tuple[str, str, datetime, datetime]] = []

    def coverage_state(self, symbols):
        completed = {
            source: {
                symbol: _dates(_launch(index).date())
                for index, symbol in enumerate(self.universe.symbols)
                if symbol in symbols
            }
            for source in DERIVATIVES_SOURCES
        }
        return BybitFullPeriodDerivativesStoredCoverage(
            completed_by_source_symbol=completed,
            unavailable_retry_after_by_source_symbol={
                source: {symbol: {} for symbol in symbols}
                for source in DERIVATIVES_SOURCES
            },
        )

    def load_open_interest(self, *, symbol, start_at, end_at):
        self.loads.append(("OPEN_INTEREST", symbol, start_at, end_at))
        return ()

    def load_account_ratio(self, *, symbol, start_at, end_at):
        self.loads.append(("ACCOUNT_RATIO", symbol, start_at, end_at))
        return ()

    def load_funding(self, *, symbol, start_at, end_at):
        self.loads.append(("FUNDING", symbol, start_at, end_at))
        return ()


def test_orchestration_uses_latest_symbol_common_start_and_one_universe_snapshot(
    monkeypatch,
) -> None:
    universe = _Universe()
    price = _PriceStore(universe)
    derivatives = _DerivativesStore(universe)
    captured: dict[str, Any] = {}

    def _fake_replay(**kwargs):
        captured.update(kwargs)
        return {
            "diagnostic": "BYBIT_SOURCE_COMMON_PERIOD_SHARED_CAPITAL_PORTFOLIO_REPLAY",
            "portfolio_competition_modeled": True,
            "historical_selection_uses_future_evidence": False,
            "bybit_live_order_routing_allowed": False,
        }

    monkeypatch.setattr(
        portfolio_tool,
        "run_source_common_period_portfolio_replay",
        _fake_replay,
    )
    report = portfolio_tool.run_source_common_period_portfolio_research(
        price,
        derivatives,
        observed_at=_OBSERVED,
        bybit_site="eu",
        opening_equity_usdt=Decimal("1000"),
        universe_client=universe,
    )

    expected_start = datetime(2026, 1, 3, tzinfo=UTC)
    expected_end = datetime(2026, 4, 10, tzinfo=UTC)
    assert universe.instrument_calls == 1
    assert universe.ticker_calls == 1
    assert captured["common_start_at"] == expected_start
    assert captured["end_exclusive_at"] == expected_end
    assert len(captured["ordered_symbols"]) == 10
    assert captured["ordered_symbols"][0] == "C00USDT"
    assert captured["ordered_symbols"][-1] == "C09USDT"
    assert set(captured["derivatives_history_by_symbol"]) == set(universe.symbols)
    assert price.load_args == {
        "symbols": tuple(sorted(universe.symbols)),
        "start_at": expected_start,
        "end_at": expected_end,
    }
    assert len(derivatives.loads) == 30
    assert report["universe_host"] == "api.bybit.eu"
    assert len(report["top10"]) == 10
    assert report["portfolio_common_start_rule"].startswith("max(")
    assert report["independent_per_symbol_evidence_remains_live_ranking_source"] is True
    assert report["portfolio_trade_evidence_is_diagnostic_only"] is True
    assert report["portfolio_trade_evidence_persisted_as_live_rank_source"] is False
    assert report["bybit_live_order_routing_allowed"] is False
