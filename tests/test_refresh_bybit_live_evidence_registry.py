from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitDerivativesHistory,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
)
from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar, BybitKlineRequest
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, evaluate_crypto_signal
from tools.refresh_bybit_live_evidence_registry import run_live_evidence_refresh

_NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
_NOW_MS = int(_NOW.timestamp() * 1000)
_DAY_MS = 86_400_000


class _UniverseClient:
    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = symbols

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
            for index, symbol in enumerate(self.symbols)
        )

    def fetch_tickers(self) -> tuple[BybitResearchTicker, ...]:
        return tuple(
            BybitResearchTicker(
                symbol=symbol,
                last_price=Decimal("200") + index,
                bid_price=Decimal("199.95") + index,
                ask_price=Decimal("200.05") + index,
                turnover_24h_usdt=Decimal("500000000") - index * Decimal("10000000"),
                volume_24h=Decimal("1000000"),
                open_interest=Decimal("500000"),
                open_interest_value_usdt=Decimal("100000000") - index * Decimal("1000000"),
                funding_rate=Decimal("0.0001"),
                price_24h_fraction=Decimal("0.02"),
            )
            for index, symbol in enumerate(self.symbols)
        )


class _KlineClient:
    def __init__(self, acquisition: BybitKlineAcquisition) -> None:
        self.acquisition = acquisition
        self.requests: list[BybitKlineRequest] = []

    def fetch(self, request: BybitKlineRequest) -> BybitKlineAcquisition:
        self.requests.append(request)
        return self.acquisition


class _DerivativesClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, int, int, str]] = []

    def fetch_history(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
        interval: str = "5min",
    ) -> BybitDerivativesHistory:
        self.requests.append((symbol, start_ms, end_ms, interval))
        return BybitDerivativesHistory(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            interval=interval,
            open_interest=(
                BybitOpenInterestPoint(
                    symbol=symbol,
                    timestamp_ms=end_ms - 10 * 60 * 1000,
                    open_interest=Decimal("100"),
                    single_open_interest=None,
                ),
                BybitOpenInterestPoint(
                    symbol=symbol,
                    timestamp_ms=end_ms - 5 * 60 * 1000,
                    open_interest=Decimal("102"),
                    single_open_interest=None,
                ),
            ),
            account_ratio=(
                BybitAccountRatioPoint(
                    symbol=symbol,
                    timestamp_ms=end_ms - 5 * 60 * 1000,
                    buy_ratio=Decimal("0.52"),
                    sell_ratio=Decimal("0.48"),
                ),
            ),
            funding=(
                BybitHistoricalFundingPoint(
                    symbol=symbol,
                    timestamp_ms=end_ms - 60 * 60 * 1000,
                    funding_rate=Decimal("0.0001"),
                ),
            ),
            request_count=3,
            host="api.bybit.eu",
        )


def _bars(symbol: str, *, rising: bool) -> tuple[BybitKlineBar, ...]:
    start = _NOW - timedelta(minutes=5 * 121)
    rows: list[BybitKlineBar] = []
    for index in range(120):
        timestamp = start + timedelta(minutes=5 * index)
        if rising:
            close = Decimal("100") + Decimal(index)
            opened = Decimal("99") + Decimal(index)
        else:
            close = Decimal("100")
            opened = Decimal("100")
        rows.append(
            BybitKlineBar(
                symbol=symbol,
                start_time=timestamp,
                open=opened,
                high=max(opened, close) + Decimal("0.4"),
                low=min(opened, close) - Decimal("0.4"),
                close=close,
                volume=Decimal("10000"),
                turnover=Decimal("2000000") + Decimal(index * 1000),
            )
        )
    return tuple(rows)


def _acquisition(symbols: tuple[str, ...]) -> BybitKlineAcquisition:
    all_bars = tuple(
        bar
        for index, symbol in enumerate(symbols)
        for bar in _bars(symbol, rising=index in {0, 4})
    )
    return BybitKlineAcquisition(
        bars=tuple(sorted(all_bars, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={symbol: 1 for symbol in symbols},
    )


def _evidence_report() -> dict[str, object]:
    return {
        "diagnostic": "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX",
        "trade_count": 0,
        "cell_count": 0,
        "minimum_cell_trades": 5,
        "turnover_reference_usdt": "1000000",
        "stress_policy": {
            "open_interest_impulse_fraction": "0.01",
            "price_shock_atr_threshold": "1.5",
            "high_stress_feature_count": 3,
            "elevated_stress_feature_count": 1,
            "feature_count": 5,
        },
        "matrix": [],
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def test_refresh_fetches_derivatives_only_for_symbols_with_current_fixed_signal() -> None:
    symbols = tuple(f"C{index:02d}USDT" for index in range(10))
    acquisition = _acquisition(symbols)
    config = CryptoPerpStrategyConfig()
    expected_signal_symbols = {
        symbol
        for symbol in symbols
        if evaluate_crypto_signal(
            tuple(bar for bar in acquisition.bars if bar.symbol == symbol),
            config,
        ).eligible
    }
    assert expected_signal_symbols
    assert expected_signal_symbols != set(symbols)

    kline = _KlineClient(acquisition)
    derivatives = _DerivativesClient()
    market, ranked = run_live_evidence_refresh(
        evidence_report=_evidence_report(),
        observed_at=_NOW,
        bybit_site="eu",
        equity_usdt=Decimal("1000"),
        equity_source="RESEARCH_REFERENCE",
        registry_limit=10,
        universe_client=_UniverseClient(symbols),
        kline_client=kline,
        derivatives_client=derivatives,
    )

    requested_derivative_symbols = {request[0] for request in derivatives.requests}
    assert requested_derivative_symbols == expected_signal_symbols
    assert len(derivatives.requests) == len(expected_signal_symbols)
    assert all(request[3] == "5min" for request in derivatives.requests)
    assert len(kline.requests) == 1
    assert kline.requests[0].symbols == tuple(item.symbol for item in market.candidates)
    assert ranked.market_snapshot_id == market.snapshot_id
    assert ranked.operator_review_required is True
    assert ranked.trade_actionable is False
    assert ranked.demo_activation_allowed is False
    assert ranked.live_activation_allowed is False
    assert ranked.bybit_live_order_routing_allowed is False
