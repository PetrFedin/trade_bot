from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.ohlcv import OhlcvBar
from tools import volatility_scaled_exits as scaled


def bar(index: int, close: float, spread: float = 0.5) -> OhlcvBar:
    price = Decimal(str(round(close, 6)))
    return OhlcvBar(
        symbol="SIMUSDT",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
        open=price,
        high=Decimal(str(round(close + spread, 6))),
        low=Decimal(str(round(close - spread, 6))),
        close=price,
        volume=1,
        trade_count=1,
    )


def test_reward_to_risk_matches_the_shipped_policy() -> None:
    """Only the stop placement may change; changing the ratio would confound the test."""
    assert scaled.REWARD_TO_RISK == pytest.approx(2.0)


def test_scaled_policy_keeps_the_shipped_ratio() -> None:
    policy = scaled.scaled_policy(0.03, 1.5)
    ratio = float(policy.take_profit_fraction / policy.stop_loss_fraction)
    assert ratio == pytest.approx(scaled.REWARD_TO_RISK)


def test_a_wider_sigma_produces_a_wider_stop() -> None:
    narrow = scaled.scaled_policy(0.01, 1.0).stop_loss_fraction
    wide = scaled.scaled_policy(0.05, 1.0).stop_loss_fraction
    assert wide > narrow


def test_stop_is_floored_and_capped() -> None:
    assert float(scaled.scaled_policy(0.0000001, 1.0).stop_loss_fraction) == scaled.MINIMUM_STOP
    assert float(scaled.scaled_policy(10.0, 1.0).stop_loss_fraction) == scaled.MAXIMUM_STOP


def test_every_scaled_policy_validates() -> None:
    for sigma in (0.001, 0.01, 0.05, 0.2):
        for multiple in (0.5, 1.0, 2.0, 4.0):
            scaled.scaled_policy(sigma, multiple).validate()


def test_trailing_sigma_uses_only_prior_bars() -> None:
    """Including the current bar would leak the outcome into the stop that judges it."""
    bars = [bar(i, 100.0 + i) for i in range(30)]
    bars.append(bar(30, 1000.0))
    calm = scaled.trailing_sigma(bars, 30)
    including = scaled.trailing_sigma(bars, 31)
    assert including > calm


def test_trailing_sigma_of_a_flat_series_is_zero() -> None:
    assert scaled.trailing_sigma([bar(i, 100.0) for i in range(30)], 25) == 0.0


def test_trailing_sigma_recovers_a_known_dispersion() -> None:
    bars = [bar(i, 100.0 * math.exp(0.01 * (i % 2))) for i in range(40)]
    assert scaled.trailing_sigma(bars, 35) > 0.004


def test_a_flat_market_produces_no_episodes() -> None:
    assert scaled.run([bar(i, 100.0) for i in range(120)], multiple=1.0) == []


def test_fixed_arm_uses_the_shipped_stop() -> None:
    bars = [bar(i, 100.0 + i * 0.6) for i in range(150)]
    results = scaled.run(bars, multiple=None)
    assert results
    assert all(r.stop_fraction == float(scaled.SHIPPED.stop_loss_fraction) for r in results)


def test_scaled_arm_varies_its_stop() -> None:
    bars = [bar(i, 100.0 + i * 0.6 + (4.0 if i > 90 else 0.0)) for i in range(160)]
    results = scaled.run(bars, multiple=1.0)
    if len(results) > 2:
        assert len({round(r.stop_fraction, 6) for r in results}) > 1


def test_first_bar_flag_agrees_with_bars_held() -> None:
    bars = [bar(i, 100.0 + i * 0.6) for i in range(150)]
    for result in scaled.run(bars, multiple=1.0):
        assert result.closed_first_bar == (result.bars_held <= 1)
