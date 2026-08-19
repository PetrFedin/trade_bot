from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    evaluate_crypto_signal,
)


def _config() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig(
        fast_ema_bars=2,
        slow_ema_bars=3,
        momentum_bars=2,
        breakout_bars=2,
        atr_bars=2,
        turnover_bars=2,
        minimum_average_turnover_usdt=Decimal("1"),
        minimum_atr_fraction=Decimal("0.0001"),
        maximum_atr_fraction=Decimal("0.10"),
        minimum_abs_momentum=Decimal("0.001"),
        minimum_quality_score=Decimal("0"),
        maximum_one_bar_atr_multiple=Decimal("10"),
    )


def _bars(closes: tuple[int, ...]) -> tuple[BybitKlineBar, ...]:
    start = datetime(2026, 8, 17, tzinfo=UTC)
    result = []
    for index, close_int in enumerate(closes):
        close = Decimal(close_int)
        result.append(
            BybitKlineBar(
                symbol="BTCUSDT",
                start_time=start + timedelta(minutes=5 * index),
                open=close - Decimal("0.5"),
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("100"),
                turnover=Decimal("1000000"),
            )
        )
    return tuple(result)


def test_default_entry_side_policy_preserves_long_and_short() -> None:
    assert CryptoPerpStrategyConfig().allowed_entry_sides == (
        CryptoSide.LONG,
        CryptoSide.SHORT,
    )


def test_entry_side_policy_fails_closed_when_empty_or_duplicate() -> None:
    with pytest.raises(ValueError, match="at least one entry side"):
        replace(_config(), allowed_entry_sides=()).validate()
    with pytest.raises(ValueError, match="must be unique"):
        replace(
            _config(),
            allowed_entry_sides=(CryptoSide.LONG, CryptoSide.LONG),
        ).validate()


def test_long_only_policy_blocks_an_otherwise_eligible_short_signal() -> None:
    short_bars = _bars((108, 107, 106, 105, 104, 103, 102, 100))
    baseline = evaluate_crypto_signal(short_bars, _config())
    assert baseline.eligible is True
    assert baseline.signal is not None
    assert baseline.signal.side is CryptoSide.SHORT

    long_only = evaluate_crypto_signal(
        short_bars,
        replace(_config(), allowed_entry_sides=(CryptoSide.LONG,)),
    )
    assert long_only.eligible is False
    assert long_only.signal is None
    assert long_only.reasons == ("ENTRY_SIDE_DISABLED_BY_POLICY",)


def test_long_only_policy_keeps_eligible_long_signal_unchanged() -> None:
    long_bars = _bars((100, 101, 102, 103, 104, 105, 106, 108))
    baseline = evaluate_crypto_signal(long_bars, _config())
    long_only = evaluate_crypto_signal(
        long_bars,
        replace(_config(), allowed_entry_sides=(CryptoSide.LONG,)),
    )

    assert baseline.eligible is True
    assert baseline.signal is not None
    assert baseline.signal.side is CryptoSide.LONG
    assert long_only == baseline
