from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_derivatives_history import BybitAccountRatioPoint
from app.marketdata.bybit_full_period_derivatives import (
    ACCOUNT_RATIO,
    DERIVATIVES_SOURCES,
)
from app.marketdata.bybit_full_period_derivatives_postgres import (
    BybitFullPeriodDerivativesStoredCoverage,
)
from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
)
from tools.backfill_bybit_full_period_derivatives import (
    run_full_period_derivatives_backfill,
)

_OBSERVED_AT = datetime(2026, 8, 23, 12, tzinfo=UTC)
_LAUNCH_AT = datetime(2026, 1, 1, tzinfo=UTC)
_FIRST_DAY = date(2026, 1, 1)


def _instrument(symbol: str) -> BybitResearchInstrument:
    return BybitResearchInstrument(
        symbol=symbol,
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
        symbol_type="innovation",
        launch_time_ms=int(_LAUNCH_AT.timestamp() * 1000),
        delivery_time_ms=0,
        is_pre_listing=False,
    )


def _ticker(symbol: str, rank_seed: int) -> BybitResearchTicker:
    return BybitResearchTicker(
        symbol=symbol,
        last_price=Decimal("100") + rank_seed,
        bid_price=Decimal("99.95") + rank_seed,
        ask_price=Decimal("100.05") + rank_seed,
        turnover_24h_usdt=Decimal("500000000") - rank_seed * Decimal("10000000"),
        volume_24h=Decimal("1000000"),
        open_interest=Decimal("1000000"),
        open_interest_value_usdt=(
            Decimal("200000000") - rank_seed * Decimal("1000000")
        ),
        funding_rate=Decimal("0.0001"),
        price_24h_fraction=Decimal("0.02"),
    )


class _FakeUniverseClient:
    def __init__(self) -> None:
        self.symbols = tuple(f"C{index:02d}USDT" for index in range(10))
        self.instruments = tuple(_instrument(symbol) for symbol in self.symbols)
        self.tickers = tuple(
            _ticker(symbol, index) for index, symbol in enumerate(self.symbols)
        )
        self.instrument_calls = 0
        self.ticker_calls = 0

    def fetch_instruments(self) -> tuple[BybitResearchInstrument, ...]:
        self.instrument_calls += 1
        return self.instruments

    def fetch_tickers(self) -> tuple[BybitResearchTicker, ...]:
        self.ticker_calls += 1
        return self.tickers


class _FakeDerivativesClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def fetch_account_ratio(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str,
    ) -> tuple[tuple[BybitAccountRatioPoint, ...], int]:
        self.calls.append(
            {
                "source": ACCOUNT_RATIO,
                "symbol": symbol,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "interval": interval,
            }
        )
        start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
        end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
        points: list[BybitAccountRatioPoint] = []
        cursor = start
        while cursor < end:
            points.append(
                BybitAccountRatioPoint(
                    symbol=symbol,
                    timestamp_ms=int(cursor.timestamp() * 1000),
                    buy_ratio=Decimal("0.55"),
                    sell_ratio=Decimal("0.45"),
                )
            )
            cursor += timedelta(minutes=5)
        return tuple(points), 1

    def fetch_open_interest(self, **_kwargs: Any):
        raise AssertionError("first sorted work item should not fetch open interest")

    def fetch_funding(self, **_kwargs: Any):
        raise AssertionError("first sorted work item should not fetch funding")


class _FakeStore:
    def __init__(self) -> None:
        self.completed = {
            source: {f"C{index:02d}USDT": set() for index in range(10)}
            for source in DERIVATIVES_SOURCES
        }
        self.unavailable = {
            source: {f"C{index:02d}USDT": {} for index in range(10)}
            for source in DERIVATIVES_SOURCES
        }
        self.complete_writes: list[tuple[str, str, date, int]] = []
        self.unavailable_writes: list[tuple[str, str, date, str]] = []
        self.migrate_calls = 0

    def migrate(self) -> None:
        self.migrate_calls += 1

    def coverage_state(
        self,
        symbols: tuple[str, ...],
    ) -> BybitFullPeriodDerivativesStoredCoverage:
        return BybitFullPeriodDerivativesStoredCoverage(
            completed_by_source_symbol={
                source: {
                    symbol: tuple(sorted(self.completed[source][symbol]))
                    for symbol in symbols
                }
                for source in DERIVATIVES_SOURCES
            },
            unavailable_retry_after_by_source_symbol={
                source: {
                    symbol: dict(self.unavailable[source][symbol])
                    for symbol in symbols
                }
                for source in DERIVATIVES_SOURCES
            },
        )

    def persist_complete_day(
        self,
        *,
        audit: Any,
        points: tuple[BybitAccountRatioPoint, ...],
        observed_at: datetime,
    ) -> str:
        assert observed_at == _OBSERVED_AT
        self.completed[audit.source][audit.symbol].add(audit.archive_date)
        self.complete_writes.append(
            (audit.source, audit.symbol, audit.archive_date, len(points))
        )
        return "a" * 64

    def persist_unavailable(self, **kwargs: Any) -> str:
        source = kwargs["source"]
        symbol = kwargs["symbol"]
        archive_date = kwargs["archive_date"]
        retry_after = kwargs["retry_after"]
        error_code = kwargs["error_code"]
        self.unavailable[source][symbol][archive_date] = retry_after
        self.unavailable_writes.append((source, symbol, archive_date, error_code))
        return "b" * 64


def test_backfill_executes_oldest_dynamic_top10_source_day_once() -> None:
    store = _FakeStore()
    universe = _FakeUniverseClient()
    derivatives = _FakeDerivativesClient()

    payload = run_full_period_derivatives_backfill(
        store=store,  # type: ignore[arg-type]
        observed_at=_OBSERVED_AT,
        bybit_site="eu",
        work_limit=1,
        migrate=True,
        universe_client=universe,  # type: ignore[arg-type]
        derivatives_client=derivatives,  # type: ignore[arg-type]
    )

    assert universe.instrument_calls == 1
    assert universe.ticker_calls == 1
    assert store.migrate_calls == 1
    assert len(payload["dynamic_top10"]) == 10
    assert payload["dynamic_top10"][0]["symbol"] == "C00USDT"
    assert payload["attempted_work_items"] == 1
    assert payload["completed_work_items"] == 1
    assert payload["unavailable_work_items"] == 0
    assert payload["public_request_count"] == 1
    assert derivatives.calls == [
        {
            "source": ACCOUNT_RATIO,
            "symbol": "C00USDT",
            "start_ms": int(_LAUNCH_AT.timestamp() * 1000),
            "end_ms": int((_LAUNCH_AT + timedelta(days=1)).timestamp() * 1000),
            "interval": "5min",
        }
    ]
    assert store.complete_writes == [(ACCOUNT_RATIO, "C00USDT", _FIRST_DAY, 288)]
    assert store.unavailable_writes == []
    assert payload["coverage_plan"]["full_period_evidence_matrix_allowed"] is False
    assert payload["full_period_evidence_matrix_allowed"] is False
    assert payload["trade_actionable"] is False
    assert payload["strategy_promotion_allowed"] is False
    assert payload["demo_activation_allowed"] is False
    assert payload["live_activation_allowed"] is False
    assert payload["bybit_live_order_routing_allowed"] is False
