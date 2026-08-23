from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_source_common_period_evidence import ArchivedBybitDerivativesHistoryView
from app.strategy.crypto_source_common_period_portfolio import (
    run_source_common_period_portfolio_replay,
)

_START = datetime(2026, 1, 1, tzinfo=UTC)
_STEP = timedelta(minutes=5)
_COUNT = 180
_SYMBOLS = ("C00USDT", "C01USDT", "C02USDT")


def _bars(symbol: str, seed: int) -> tuple[BybitKlineBar, ...]:
    rows: list[BybitKlineBar] = []
    base = Decimal("100") + Decimal(seed * 10)
    for index in range(_COUNT):
        opened = base + Decimal(index) * Decimal("0.20")
        close = opened + Decimal("0.12")
        rows.append(
            BybitKlineBar(
                symbol=symbol,
                start_time=_START + index * _STEP,
                open=opened,
                high=close + Decimal("0.12"),
                low=opened - Decimal("0.12"),
                close=close,
                volume=Decimal("10000"),
                turnover=Decimal("2000000") + Decimal(index * 1000),
            )
        )
    return tuple(rows)


def _derivatives(symbol: str) -> ArchivedBybitDerivativesHistoryView:
    oi = tuple(
        BybitOpenInterestPoint(
            symbol=symbol,
            timestamp_ms=int((_START + index * _STEP).timestamp() * 1000),
            open_interest=Decimal("100000") + Decimal(index * 100),
            single_open_interest=None,
        )
        for index in range(_COUNT)
    )
    ratio = tuple(
        BybitAccountRatioPoint(
            symbol=symbol,
            timestamp_ms=int((_START + index * _STEP).timestamp() * 1000),
            buy_ratio=Decimal("0.55"),
            sell_ratio=Decimal("0.45"),
        )
        for index in range(_COUNT)
    )
    funding = tuple(
        BybitHistoricalFundingPoint(
            symbol=symbol,
            timestamp_ms=int((_START + hours * timedelta(hours=1)).timestamp() * 1000),
            funding_rate=Decimal("0.0001"),
        )
        for hours in (0, 8)
    )
    history = ArchivedBybitDerivativesHistoryView(
        symbol=symbol,
        start_ms=int(_START.timestamp() * 1000),
        end_ms=int((_START + _COUNT * _STEP).timestamp() * 1000) - 1,
        interval="5min",
        open_interest=oi,
        account_ratio=ratio,
        funding=funding,
    )
    history.validate()
    return history


def test_shared_capital_replay_models_competition_without_future_evidence() -> None:
    bars = {symbol: _bars(symbol, index) for index, symbol in enumerate(_SYMBOLS)}
    histories = {symbol: _derivatives(symbol) for symbol in _SYMBOLS}

    report = run_source_common_period_portfolio_replay(
        ordered_symbols=_SYMBOLS,
        bars_by_symbol=bars,
        common_start_at=_START,
        end_exclusive_at=_START + _COUNT * _STEP,
        opening_equity_usdt=Decimal("1000"),
        derivatives_history_by_symbol=histories,
    )

    assert report["portfolio_competition_modeled"] is True
    assert report["shared_capital_modeled"] is True
    assert report["historical_selection_uses_future_evidence"] is False
    assert report["evidence_used_for_historical_selection"] is False
    assert report["selection_uses_point_in_time_price_signal_only"] is True
    assert report["derivatives_used_for_post_replay_attribution_only"] is True
    assert report["synchronized_bar_count_per_symbol"] == _COUNT
    assert report["eligible_signal_event_count"] > 0
    assert report["accepted_trade_plan_event_count"] > 0
    assert report["concurrency_block_count"] > 0
    assert report["portfolio_metrics"]["maximum_concurrent_positions"] == 2
    assert len(report["per_symbol"]) == 3
    assert [row["market_rank_at_research_time"] for row in report["per_symbol"]] == [1, 2, 3]
    assert report["maximum_initial_gross_notional_usdt"] != "0"

    evidence = report["portfolio_trade_evidence_matrix"]
    assert evidence is not None
    assert evidence["diagnostic"] == "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX"
    assert evidence["trade_count"] == report["portfolio_metrics"]["closed_trade_count"]
    assert evidence["historical_selection_uses_future_evidence"] is False
    assert evidence["evidence_used_for_historical_selection"] is False
    assert evidence["portfolio_competition_modeled"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert report["trade_actionable"] is False
    assert report["operator_review_required"] is True
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False


def test_missing_single_bar_fails_closed_instead_of_intersecting_it_away() -> None:
    bars = {symbol: _bars(symbol, index) for index, symbol in enumerate(_SYMBOLS)}
    bars["C02USDT"] = bars["C02USDT"][:80] + bars["C02USDT"][81:]

    with pytest.raises(ValueError, match="price grid count mismatch"):
        run_source_common_period_portfolio_replay(
            ordered_symbols=_SYMBOLS,
            bars_by_symbol=bars,
            common_start_at=_START,
            end_exclusive_at=_START + _COUNT * _STEP,
        )


def test_custom_strategy_is_rejected_before_historical_portfolio_selection() -> None:
    bars = {symbol: _bars(symbol, index) for index, symbol in enumerate(_SYMBOLS)}
    custom = replace(
        CryptoPerpStrategyConfig(),
        maximum_concurrent_positions=3,
    )

    with pytest.raises(ValueError, match="qualified fixed strategy config"):
        run_source_common_period_portfolio_replay(
            ordered_symbols=_SYMBOLS,
            bars_by_symbol=bars,
            common_start_at=_START,
            end_exclusive_at=_START + _COUNT * _STEP,
            strategy_config=custom,
        )
