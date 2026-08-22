from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_market_history_profile import (
    CryptoMarketHistoryPolicy,
    profile_crypto_market_history,
)

_START = datetime(2025, 1, 1, tzinfo=UTC)


def _bars(symbol: str, closes: list[Decimal]) -> tuple[BybitKlineBar, ...]:
    result: list[BybitKlineBar] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_price = previous if index else close
        high = max(open_price, close) + Decimal("0.5")
        low = min(open_price, close) - Decimal("0.5")
        result.append(
            BybitKlineBar(
                symbol=symbol,
                start_time=_START + timedelta(hours=index),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=Decimal("1000"),
                turnover=Decimal("1000000") + Decimal(index * 1000),
            )
        )
        previous = close
    return tuple(result)


def _acquisition() -> BybitKlineAcquisition:
    base = [Decimal("100") + Decimal(index) for index in range(80)]
    base.extend(Decimal("179") - Decimal(index) for index in range(1, 81))
    eth = [value * Decimal("2") for value in base]
    inverse = [Decimal("400") - value for value in base]
    rows = (
        *_bars("BTCUSDT", base),
        *_bars("ETHUSDT", eth),
        *_bars("SOLUSDT", inverse),
    )
    return BybitKlineAcquisition(
        bars=tuple(sorted(rows, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 1},
    )


def test_full_history_profile_reports_regimes_drawdown_and_correlations() -> None:
    report = profile_crypto_market_history(
        _acquisition(),
        policy=CryptoMarketHistoryPolicy(
            fast_ema_bars=9,
            slow_ema_bars=21,
            momentum_bars=6,
            atr_bars=7,
            minimum_regime_episode_bars=3,
            minimum_correlation_observations=20,
        ),
    )

    assert report["symbol_count"] == 3
    assert report["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    btc = report["symbol_profiles"]["BTCUSDT"]
    assert btc["bar_count"] == 160
    assert btc["maximum_drawdown_fraction"] > 0
    assert btc["current_regime"] in {"BULL_TREND", "BEAR_TREND", "RANGE_TRANSITION"}
    assert report["regime_episode_summary"]
    correlations = {
        item["pair"]: item["correlation"]
        for item in report["pairwise_return_correlations"]
    }
    assert correlations["BTCUSDT:ETHUSDT"] == pytest.approx(1.0, abs=1e-12)
    assert correlations["BTCUSDT:SOLUSDT"] < 0
    assert report["strategy_parameters_changed"] is False
    assert report["strategy_selection_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False
    assert report["causal_claim_allowed"] is False
    assert report["predictive_guarantee_allowed"] is False


def test_full_history_profile_rejects_insufficient_history() -> None:
    short = tuple(
        sorted(
            (*_bars("BTCUSDT", [Decimal("100") + index for index in range(10)]),
             *_bars("ETHUSDT", [Decimal("200") + index for index in range(10)])),
            key=lambda item: (item.symbol, item.start_time),
        )
    )
    acquisition = BybitKlineAcquisition(
        bars=short,
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1},
    )
    with pytest.raises(ValueError, match="insufficient bars"):
        profile_crypto_market_history(
            acquisition,
            policy=CryptoMarketHistoryPolicy(
                fast_ema_bars=5,
                slow_ema_bars=12,
                momentum_bars=3,
                atr_bars=3,
                minimum_regime_episode_bars=1,
                minimum_correlation_observations=3,
            ),
        )
