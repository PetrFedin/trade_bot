from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_correlation import (
    CryptoCorrelationPolicy,
    crypto_return_correlation,
    evaluate_crypto_correlation,
)


def _series(symbol: str, closes: list[Decimal]) -> list[BybitKlineBar]:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    return [
        BybitKlineBar(
            symbol=symbol,
            start_time=start + timedelta(minutes=5 * index),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=Decimal("1"),
            turnover=close,
        )
        for index, close in enumerate(closes)
    ]


def _closes_from_returns(start: str, returns: list[str]) -> list[Decimal]:
    closes = [Decimal(start)]
    for value in returns:
        closes.append(closes[-1] * (Decimal("1") + Decimal(value)))
    return closes


def test_identical_return_stream_is_blocked_as_duplicate_exposure() -> None:
    returns = [
        "0.01",
        "0.02",
        "-0.005",
        "0.015",
        "0.01",
        "-0.01",
        "0.02",
        "0.005",
        "-0.004",
        "0.012",
    ]
    btc = _series("BTCUSDT", _closes_from_returns("100", returns))
    eth = _series("ETHUSDT", _closes_from_returns("200", returns))

    decision = evaluate_crypto_correlation(
        "ETHUSDT",
        selected_symbols=("BTCUSDT",),
        histories={"BTCUSDT": btc, "ETHUSDT": eth},
        policy=CryptoCorrelationPolicy(
            lookback_bars=10,
            minimum_return_observations=10,
            maximum_pairwise_correlation=Decimal("0.85"),
        ),
    )

    assert decision.eligible is False
    assert decision.reason == "PAIRWISE_CORRELATION_ABOVE_LIMIT"
    assert decision.blocking_symbol == "BTCUSDT"
    assert decision.correlation == Decimal("1")
    assert decision.demo_activation_allowed is False
    assert decision.live_activation_allowed is False


def test_negative_correlation_is_not_penalized() -> None:
    returns = [
        "0.01",
        "0.02",
        "-0.005",
        "0.015",
        "0.01",
        "-0.01",
        "0.02",
        "0.005",
        "-0.004",
        "0.012",
    ]
    opposite = [str(-Decimal(value)) for value in returns]
    btc = _series("BTCUSDT", _closes_from_returns("100", returns))
    hedge = _series("ETHUSDT", _closes_from_returns("200", opposite))

    correlation = crypto_return_correlation(
        btc,
        hedge,
        lookback_bars=10,
        minimum_return_observations=10,
    )
    decision = evaluate_crypto_correlation(
        "ETHUSDT",
        selected_symbols=("BTCUSDT",),
        histories={"BTCUSDT": btc, "ETHUSDT": hedge},
        policy=CryptoCorrelationPolicy(
            lookback_bars=10,
            minimum_return_observations=10,
            maximum_pairwise_correlation=Decimal("0.85"),
        ),
    )

    assert correlation < 0
    assert decision.eligible is True
    assert decision.reason is None


def test_peer_without_enough_history_fails_closed() -> None:
    btc = _series("BTCUSDT", [Decimal("100"), Decimal("101"), Decimal("102")])
    eth = _series("ETHUSDT", [Decimal("200"), Decimal("201"), Decimal("202")])

    decision = evaluate_crypto_correlation(
        "ETHUSDT",
        selected_symbols=("BTCUSDT",),
        histories={"BTCUSDT": btc, "ETHUSDT": eth},
        policy=CryptoCorrelationPolicy(
            lookback_bars=10,
            minimum_return_observations=5,
        ),
    )

    assert decision.eligible is False
    assert decision.reason == "CORRELATION_HISTORY_INSUFFICIENT"
    assert decision.blocking_symbol == "BTCUSDT"
    assert decision.correlation is None


def test_correlation_policy_rejects_invalid_threshold() -> None:
    with pytest.raises(ValueError):
        CryptoCorrelationPolicy(maximum_pairwise_correlation=Decimal("1.01")).validate()