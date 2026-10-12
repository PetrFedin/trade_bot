"""P0 research only: prior completed regime classification and PnL conservation."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.historical_portfolio_regime_attribution import (
    prior_completed_btc_regime,
    run_market_regime_attribution,
)

BAR_DIR = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
SIX = ("BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
BROAD = (
    "ADAUSDT", "APTUSDT", "ARBUSDT", "ATOMUSDT", "AVAXUSDT",
    "BNBUSDT", "BTCUSDT", "DOGEUSDT", "DOTUSDT", "ETHUSDT",
    "FILUSDT", "INJUSDT", "LINKUSDT", "LTCUSDT", "NEARUSDT",
    "OPUSDT", "SOLUSDT", "SUIUSDT", "TONUSDT", "TRXUSDT", "XRPUSDT",
)


def _bars(factors: list[str]) -> tuple:
    price = Decimal("100")
    items = [SimpleNamespace(close=price)]
    for factor in factors:
        price *= Decimal(factor)
        items.append(SimpleNamespace(close=price))
    return tuple(items)


def test_prior_btc_bull_and_low_volatility() -> None:
    bars = _bars(["1.005"] * 32)
    trend, volatility = prior_completed_btc_regime(bars, len(bars) - 1)
    assert trend == "BULL"
    assert volatility == "LOW_VOL"


def test_prior_btc_bear_and_low_volatility() -> None:
    bars = _bars(["0.995"] * 32)
    trend, volatility = prior_completed_btc_regime(bars, len(bars) - 1)
    assert trend == "BEAR"
    assert volatility == "LOW_VOL"


def test_sideways_high_volatility_is_possible() -> None:
    bars = _bars(["1.04", "0.96"] * 16)
    trend, volatility = prior_completed_btc_regime(bars, len(bars) - 1)
    assert trend == "SIDEWAYS"
    assert volatility == "HIGH_VOL"


def test_execution_bar_future_movement_cannot_relabel_regime() -> None:
    bars = _bars(["1.005"] * 32)
    regime_before = prior_completed_btc_regime(bars, 32)
    altered = tuple(bars[:32]) + (SimpleNamespace(close=Decimal("0.00001")),)
    regime_after = prior_completed_btc_regime(altered, 32)
    assert regime_before == regime_after
    assert regime_before == ("BULL", "LOW_VOL")


def test_no_context_is_explicit_and_out_of_range_is_rejected() -> None:
    bars = _bars(["1.005"] * 32)
    assert prior_completed_btc_regime(bars, 30) == (
        "INSUFFICIENT_PRIOR_CONTEXT", "INSUFFICIENT_PRIOR_CONTEXT"
    )
    with pytest.raises(ValueError, match="outside BTC history"):
        prior_completed_btc_regime(bars, len(bars))


@pytest.mark.parametrize(
    ("symbols", "expected_trades", "expected_return"),
    [
        (SIX, 256, Decimal("0.007515525902184908204853186")),
        (BROAD, 276, Decimal("-0.1797085857706086935251021902")),
    ],
)
def test_real_shipped_portfolio_regime_buckets_conserve_marked_equity(
    symbols: tuple[str, ...],
    expected_trades: int,
    expected_return: Decimal,
) -> None:
    report = run_market_regime_attribution(bars_dir=BAR_DIR, symbols=symbols)

    assert report["evaluation_class"] == (
        "REUSED_INSPECTED_HISTORY_REGIME_DIAGNOSTIC_ONLY"
    )
    assert report["strategy_promotion_allowed"] is False
    assert report["live_trading_allowed"] is False
    assert report["untouched_out_of_sample"] is False
    assert report["strategy_parameters_changed"] is False
    assert report["portfolio"]["closed_trades"] == expected_trades
    assert report["portfolio"]["marked_pnl_conserved"] is True
    assert report["portfolio"]["closed_trades_conserved"] is True
    assert abs(
        Decimal(report["portfolio"]["portfolio_return_before_funding"])
        - expected_return
    ) <= Decimal("0.000000001")

    total = Decimal(report["portfolio"]["net_portfolio_pnl_usd_before_funding"])
    number_days = int(report["portfolio"]["marked_days"])
    for dimension in ("by_trend", "by_volatility", "by_trend_volatility"):
        groups = report[dimension]
        assert sum(int(row["days"]) for row in groups.values()) == number_days
        assert sum(int(row["closed_trades"]) for row in groups.values()) == expected_trades
        group_pnl = sum(
            (Decimal(row["net_portfolio_pnl_usd_before_funding"])
             for row in groups.values()),
            Decimal("0"),
        )
        assert abs(group_pnl - total) <= Decimal("0.000001")
        assert all(
            int(row["positive_days"]) + int(row["negative_days"])
            + int(row["flat_days"]) == int(row["days"])
            for row in groups.values()
        )
    assert report["definition"]["regime_at_execution_time_has_no_current_execution_bar_leakage"] is True
    assert len(report["data_sha256"]) == len(symbols)
    assert len(report["evidence_sha256"]) == 64
