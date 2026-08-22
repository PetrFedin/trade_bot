from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
    BybitResearchUniversePolicy,
)
from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar, BybitKlineRequest
from tools.qualify_bybit_crypto_walk_forward import CryptoWalkForwardPolicy
from tools.research_bybit_dynamic_top10 import run_dynamic_top10_research

_NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
_NOW_MS = int(_NOW.timestamp() * 1000)
_DAY_MS = 86_400_000
_SYMBOLS = tuple(f"C{index}USDT" for index in range(10))


class _UniverseClient:
    def fetch_instruments(self) -> tuple[BybitResearchInstrument, ...]:
        return tuple(
            BybitResearchInstrument(
                symbol=symbol,
                base_coin=symbol.removesuffix("USDT"),
                quote_coin="USDT",
                settle_coin="USDT",
                contract_type="LinearPerpetual",
                status="Trading",
                symbol_type="innovation",
                launch_time_ms=_NOW_MS - (500 + index) * _DAY_MS,
                delivery_time_ms=0,
                is_pre_listing=False,
            )
            for index, symbol in enumerate(_SYMBOLS)
        )

    def fetch_tickers(self) -> tuple[BybitResearchTicker, ...]:
        return tuple(
            BybitResearchTicker(
                symbol=symbol,
                last_price=Decimal("100") + index,
                bid_price=Decimal("99.99") + index,
                ask_price=Decimal("100.01") + index,
                turnover_24h_usdt=Decimal("100000000") - index * Decimal("1000000"),
                volume_24h=Decimal("1000000"),
                open_interest=Decimal("500000"),
                open_interest_value_usdt=Decimal("50000000") - index * Decimal("500000"),
                funding_rate=Decimal("0.0001"),
                price_24h_fraction=Decimal("0.01"),
            )
            for index, symbol in enumerate(_SYMBOLS)
        )


class _ArchiveResult:
    def __init__(self, klines: BybitKlineAcquisition) -> None:
        self.klines = klines

    def validate(
        self,
        *,
        requested_symbols: tuple[str, ...],
        minimum_bars: int,
    ) -> None:
        self.klines.validate(
            requested_symbols=requested_symbols,
            minimum_bars=minimum_bars,
        )


class _ArchiveClient:
    def __init__(self, klines: BybitKlineAcquisition) -> None:
        self.klines = klines
        self.requests: list[tuple[tuple[str, ...], tuple[date, ...], int]] = []

    def fetch_klines(
        self,
        *,
        symbols: tuple[str, ...],
        dates: tuple[date, ...],
        interval_minutes: int,
    ) -> _ArchiveResult:
        self.requests.append((symbols, dates, interval_minutes))
        return _ArchiveResult(self.klines)


class _KlineClient:
    def __init__(self, acquisition: BybitKlineAcquisition) -> None:
        self.acquisition = acquisition
        self.requests: list[BybitKlineRequest] = []

    def fetch(self, request: BybitKlineRequest) -> BybitKlineAcquisition:
        self.requests.append(request)
        return self.acquisition


def _bars(
    symbol: str,
    times: list[datetime],
    *,
    rising: bool,
) -> tuple[BybitKlineBar, ...]:
    rows: list[BybitKlineBar] = []
    for index, timestamp in enumerate(times):
        close = (
            Decimal("100") + Decimal(index)
            if rising
            else Decimal("300") - Decimal(index)
        )
        previous = (
            Decimal("99") + Decimal(index)
            if rising
            else Decimal("301") - Decimal(index)
        )
        rows.append(
            BybitKlineBar(
                symbol=symbol,
                start_time=timestamp,
                open=previous,
                high=max(previous, close) + Decimal("0.4"),
                low=min(previous, close) - Decimal("0.4"),
                close=close,
                volume=Decimal("10000"),
                turnover=Decimal("2000000") + Decimal(index * 1000),
            )
        )
    return tuple(rows)


def _macro_acquisition() -> BybitKlineAcquisition:
    times = [_NOW - timedelta(hours=120 - index) for index in range(120)]
    rows = tuple(
        bar
        for symbol_index, symbol in enumerate(_SYMBOLS)
        for bar in _bars(symbol, times, rising=symbol_index % 2 == 0)
    )
    return BybitKlineAcquisition(
        bars=tuple(sorted(rows, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={symbol: 1 for symbol in _SYMBOLS},
    )


def _micro_acquisition() -> BybitKlineAcquisition:
    day_one = (_NOW - timedelta(days=2)).date()
    day_two = (_NOW - timedelta(days=1)).date()
    times: list[datetime] = []
    for value in (day_one, day_two):
        start = datetime(value.year, value.month, value.day, tzinfo=UTC)
        times.extend(start + timedelta(minutes=5 * index) for index in range(36))
    rows = tuple(
        bar
        for symbol_index, symbol in enumerate(_SYMBOLS)
        for bar in _bars(symbol, times, rising=symbol_index % 2 == 0)
    )
    return BybitKlineAcquisition(
        bars=tuple(sorted(rows, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={symbol: 1 for symbol in _SYMBOLS},
    )


def test_one_command_pipeline_keeps_top10_history_walk_forward_and_safety_boundaries() -> None:
    archive = _ArchiveClient(_micro_acquisition())
    kline = _KlineClient(_macro_acquisition())
    report = run_dynamic_top10_research(
        observed_at=_NOW,
        bybit_site="eu",
        opening_equity_usdt=Decimal("1000"),
        micro_lookback_days=2,
        universe_policy=BybitResearchUniversePolicy(
            top_n=10,
            minimum_listing_days=30,
            minimum_turnover_24h_usdt=Decimal("1000000"),
            minimum_open_interest_value_usdt=Decimal("1000000"),
            maximum_spread_bps=Decimal("100"),
            maximum_abs_funding_rate=Decimal("0.01"),
        ),
        walk_forward_policy=CryptoWalkForwardPolicy(
            fold_days=1,
            minimum_folds=2,
            minimum_total_closed_trades=1,
        ),
        universe_client=_UniverseClient(),
        archive_client=archive,
        kline_client=kline,
    )

    assert report["top10_symbols"] == list(_SYMBOLS)
    assert report["public_universe_host"] == "api.bybit.eu"
    assert report["universe"]["complete_top_n"] is True
    assert report["full_history_hourly"]["symbol_count"] == 10
    assert report["micro_execution_history"]["lookback_days"] == 2
    assert report["strategy_walk_forward"]["fold_count"] == 2
    assert "CONDITIONAL_COMBINED_RISK" in report["strategy_candidate_comparison"]
    assert report["combined_risk_trade_conditions"]["causal_claim_allowed"] is False
    assert report["known_next_evidence_gaps"]
    assert report["parameter_retuning_performed"] is False
    assert report["strategy_selection_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False
    assert report["real_money_order_submission_supported"] is False
    assert archive.requests[0][0] == _SYMBOLS
    assert archive.requests[0][2] == 5
    assert kline.requests[0].symbols == _SYMBOLS
    assert kline.requests[0].interval == "60"


def test_pipeline_rejects_non_top10_policy_and_unknown_site() -> None:
    try:
        run_dynamic_top10_research(
            observed_at=_NOW,
            bybit_site="eu",
            universe_policy=BybitResearchUniversePolicy(top_n=9),
            universe_client=_UniverseClient(),
            archive_client=_ArchiveClient(_micro_acquisition()),
            kline_client=_KlineClient(_macro_acquisition()),
        )
    except ValueError as exc:
        assert "top_n=10" in str(exc)
    else:
        raise AssertionError("non-Top-10 policy must fail")

    try:
        run_dynamic_top10_research(
            observed_at=_NOW,
            bybit_site="arbitrary-host",
            universe_client=_UniverseClient(),
            archive_client=_ArchiveClient(_micro_acquisition()),
            kline_client=_KlineClient(_macro_acquisition()),
        )
    except ValueError as exc:
        assert "site must be one of" in str(exc)
    else:
        raise AssertionError("arbitrary Bybit research host must fail")
