from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.trading import Bar
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy

START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


def bars(values: list[str]) -> list[Bar]:
    return [
        Bar("AAPL", START + timedelta(minutes=index), Decimal(value))
        for index, value in enumerate(values)
    ]


def test_clean_uptrend_requires_multiple_confirmations() -> None:
    strategy = RegimeAwareMomentumStrategy()
    sample = bars(["100", "101", "102", "103", "104", "105", "106", "107"])
    signal = strategy.signal(sample)
    assert signal.eligible
    assert signal.reasons == ()
    assert signal.momentum_return > 0
    assert signal.trend_strength > 0
    assert strategy.target(sample).quantity == Decimal("1")


def test_downtrend_stays_flat() -> None:
    strategy = RegimeAwareMomentumStrategy()
    sample = bars(["107", "106", "105", "104", "103", "102", "101", "100"])
    signal = strategy.signal(sample)
    assert not signal.eligible
    assert "FAST_AVERAGE_NOT_ABOVE_SLOW" in signal.reasons
    assert "MOMENTUM_BELOW_MINIMUM" in signal.reasons
    assert strategy.target(sample).quantity == Decimal("0")


def test_volatile_spike_is_not_treated_as_clean_momentum() -> None:
    strategy = RegimeAwareMomentumStrategy()
    signal = strategy.signal(
        bars(["100", "101", "100", "101", "100", "101", "100", "120"])
    )
    assert not signal.eligible
    assert "REALIZED_VOLATILITY_ABOVE_LIMIT" in signal.reasons
