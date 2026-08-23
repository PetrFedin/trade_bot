from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import tools.research_bybit_source_common_period_evidence as research_module
from app.marketdata.bybit_full_period_5m_postgres import BybitFullPeriod5mStoredCoverage
from app.marketdata.bybit_full_period_derivatives import DERIVATIVES_SOURCES
from app.marketdata.bybit_full_period_derivatives_postgres import (
    BybitFullPeriodDerivativesStoredCoverage,
)
from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
)
from app.strategy.crypto_strategy_evidence_matrix import CryptoStrategyEvidenceRow
from tools.research_bybit_source_common_period_evidence import (
    run_source_common_period_evidence_research,
)

_OBSERVED_AT = datetime(2026, 8, 23, 12, tzinfo=UTC)
_LAUNCH_AT = datetime(2026, 1, 1, tzinfo=UTC)
_LAST_ARCHIVE_DAY = date(2026, 8, 22)


def _dates() -> tuple[date, ...]:
    count = (_LAST_ARCHIVE_DAY - _LAUNCH_AT.date()).days + 1
    return tuple(_LAUNCH_AT.date() + timedelta(days=index) for index in range(count))


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


def _ticker(symbol: str, index: int) -> BybitResearchTicker:
    return BybitResearchTicker(
        symbol=symbol,
        last_price=Decimal("100") + index,
        bid_price=Decimal("99.95") + index,
        ask_price=Decimal("100.05") + index,
        turnover_24h_usdt=Decimal("500000000") - index * Decimal("10000000"),
        volume_24h=Decimal("1000000"),
        open_interest=Decimal("1000000"),
        open_interest_value_usdt=(
            Decimal("200000000") - index * Decimal("1000000")
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

    def fetch_instruments(self) -> tuple[BybitResearchInstrument, ...]:
        return self.instruments

    def fetch_tickers(self) -> tuple[BybitResearchTicker, ...]:
        return self.tickers


class _FakePriceStore:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = tuple(sorted(symbols))
        self.load_calls: list[tuple[str, datetime | None, datetime | None]] = []

    def coverage_state(
        self,
        symbols: tuple[str, ...],
    ) -> BybitFullPeriod5mStoredCoverage:
        assert symbols == self.symbols
        dates = _dates()
        return BybitFullPeriod5mStoredCoverage(
            completed_by_symbol={symbol: dates for symbol in symbols},
            unavailable_retry_after_by_symbol={symbol: {} for symbol in symbols},
        )

    def load_bars(
        self,
        *,
        symbols: tuple[str, ...],
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> tuple[()]:
        assert len(symbols) == 1
        self.load_calls.append((symbols[0], start_at, end_at))
        return ()


class _FakeDerivativesStore:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = tuple(sorted(symbols))
        self.load_calls: list[tuple[str, str, datetime, datetime]] = []

    def coverage_state(
        self,
        symbols: tuple[str, ...],
    ) -> BybitFullPeriodDerivativesStoredCoverage:
        assert symbols == self.symbols
        dates = _dates()
        return BybitFullPeriodDerivativesStoredCoverage(
            completed_by_source_symbol={
                source: {symbol: dates for symbol in symbols}
                for source in DERIVATIVES_SOURCES
            },
            unavailable_retry_after_by_source_symbol={
                source: {symbol: {} for symbol in symbols}
                for source in DERIVATIVES_SOURCES
            },
        )

    def load_open_interest(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[()]:
        self.load_calls.append(("OPEN_INTEREST", symbol, start_at, end_at))
        return ()

    def load_account_ratio(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[()]:
        self.load_calls.append(("ACCOUNT_RATIO", symbol, start_at, end_at))
        return ()

    def load_funding(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[()]:
        self.load_calls.append(("FUNDING", symbol, start_at, end_at))
        return ()


class _FakeEvidenceStore:
    def __init__(self) -> None:
        self.persisted: dict[str, Any] | None = None
        self.observed_at: datetime | None = None

    def persist_evidence_report(
        self,
        report: dict[str, Any],
        *,
        observed_at: datetime,
    ) -> str:
        self.persisted = dict(report)
        self.observed_at = observed_at
        return "e" * 64


def _row(symbol: str, index: int) -> CryptoStrategyEvidenceRow:
    decision = _LAUNCH_AT + timedelta(days=30 + index, hours=1)
    entry = decision + timedelta(minutes=5)
    exit_at = entry + timedelta(minutes=30)
    return CryptoStrategyEvidenceRow(
        symbol=symbol,
        side="LONG",
        decision_time=decision.isoformat(),
        entry_time=entry.isoformat(),
        exit_time=exit_at.isoformat(),
        exit_reason="TARGET",
        net_pnl_usdt=Decimal("12.5"),
        maximum_favorable_r=Decimal("1.8"),
        maximum_adverse_r=Decimal("-0.3"),
        market_regime=(
            "VOL_MID_NORMAL|TREND_STRONG|BREAKOUT_CONFIRMED|TURNOVER_HIGH"
        ),
        volatility_regime="VOL_MID_NORMAL",
        trend_regime="TREND_STRONG",
        breakout_regime="BREAKOUT_CONFIRMED",
        turnover_regime="TURNOVER_HIGH",
        open_interest_regime="OI_RISING",
        crowding_regime="LONG_HEAVY",
        prior_funding_regime="FUNDING_POSITIVE",
        stress_regime="STRESS_ELEVATED",
        stress_score=3,
        stress_feature_complete=True,
        stress_reasons=(
            "OPEN_INTEREST_IMPULSE",
            "POSITION_HOLDER_CROWDING",
            "CROWDED_SIDE_PAYS_PRIOR_FUNDING",
        ),
        open_interest_delta_fraction=Decimal("0.02"),
        long_account_ratio=Decimal("0.60"),
        prior_funding_rate=Decimal("0.0001"),
        atr_fraction=Decimal("0.01"),
        one_bar_atr_multiple=Decimal("0.8"),
        quality_score=Decimal("1.4"),
        average_turnover_usdt=Decimal("2000000") + index,
        expected_net_edge_usd=Decimal("30"),
        modeled_round_trip_cost_usdt=Decimal("0.6"),
        cost_to_expected_edge=Decimal("0.02"),
        expected_edge_to_risk=Decimal("2"),
    )


def test_source_common_research_builds_persistable_top10_matrix(monkeypatch) -> None:
    universe = _FakeUniverseClient()
    price_store = _FakePriceStore(universe.symbols)
    derivatives_store = _FakeDerivativesStore(universe.symbols)
    evidence_store = _FakeEvidenceStore()
    by_symbol_index = {symbol: index for index, symbol in enumerate(universe.symbols)}

    def fake_build(
        instrument: BybitResearchInstrument,
        **kwargs: Any,
    ) -> tuple[tuple[CryptoStrategyEvidenceRow, ...], dict[str, Any]]:
        index = by_symbol_index[instrument.symbol]
        assert kwargs["common_start_at"] == _LAUNCH_AT
        assert kwargs["end_exclusive_at"] == datetime(2026, 8, 23, tzinfo=UTC)
        row = _row(instrument.symbol, index)
        row.validate()
        return (row,), {
            "symbol": instrument.symbol,
            "common_start_at": _LAUNCH_AT.isoformat(),
            "end_exclusive_at": datetime(2026, 8, 23, tzinfo=UTC).isoformat(),
            "closed_trade_count": 1,
            "scope": "MAX_SOURCE_AVAILABLE_COMMON_PERIOD",
            "strategy_parameters_changed": False,
            "strategy_promotion_allowed": False,
            "bybit_live_order_routing_allowed": False,
        }

    monkeypatch.setattr(
        research_module,
        "build_source_common_period_symbol_evidence_rows",
        fake_build,
    )

    report = run_source_common_period_evidence_research(
        price_store,
        derivatives_store,
        observed_at=_OBSERVED_AT,
        bybit_site="eu",
        universe_client=universe,
        evidence_store=evidence_store,
    )

    assert report["diagnostic"] == "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX"
    assert report["evidence_scope"] == "PER_SYMBOL_MAX_SOURCE_AVAILABLE_COMMON_PERIOD"
    assert report["trade_count"] == 10
    assert report["top10_symbols"] == list(universe.symbols)
    assert report["price_history_full_period_complete"] is True
    assert report["derivatives_source_available_period_complete"] is True
    assert report["instrument_lifetime_derivatives_complete"] is True
    assert report["instrument_lifetime_combined_matrix_claim_allowed"] is True
    assert report["source_available_common_period_matrix"] is True
    assert report["portfolio_competition_modeled"] is False
    assert report["strategy_parameters_changed"] is False
    assert report["parameter_retuning_performed"] is False
    assert report["strategy_selection_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False
    assert report["persisted_evidence_snapshot_id"] == "e" * 64

    assert evidence_store.observed_at == _OBSERVED_AT
    assert evidence_store.persisted is not None
    assert "persisted_evidence_snapshot_id" not in evidence_store.persisted
    assert evidence_store.persisted["trade_count"] == 10
    assert len(price_store.load_calls) == 10
    assert len(derivatives_store.load_calls) == 30
