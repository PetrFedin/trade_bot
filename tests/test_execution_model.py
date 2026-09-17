from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.ohlcv import OhlcvBar
from tools import execution_model as execution


def bar(index: int, close: float, *, low: float | None = None, high: float | None = None):
    price = Decimal(str(round(close, 6)))
    return OhlcvBar(
        symbol="SIMUSDT",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
        open=price,
        high=Decimal(str(round(high if high is not None else close + 0.5, 6))),
        low=Decimal(str(round(low if low is not None else close - 0.5, 6))),
        close=price,
        volume=1,
        trade_count=1,
    )


def rising(n: int = 200, spread: float = 0.5) -> list[OhlcvBar]:
    return [bar(i, 100.0 + i * 0.4, low=100.0 + i * 0.4 - spread) for i in range(n)]


def test_maker_fee_is_cheaper_than_taker() -> None:
    """If this inverts, the whole comparison is upside down."""
    assert execution.MAKER_ROUND_TRIP < execution.TAKER_ROUND_TRIP


def test_a_bid_that_price_never_reaches_does_not_fill() -> None:
    """Resting far below a rising market must record misses, not free fills."""
    report = execution.simulate(rising(spread=0.1), offset=0.05)
    assert report["fill_rate"] == 0.0
    assert report["maker_when_filled"]["episodes"] == 0


def test_a_bid_at_the_reference_always_fills() -> None:
    report = execution.simulate(rising(), offset=0.0)
    assert report["fill_rate"] == 1.0


def test_fill_rate_falls_as_the_bid_moves_away() -> None:
    near = execution.simulate(rising(), offset=0.001)["fill_rate"]
    far = execution.simulate(rising(), offset=0.02)["fill_rate"]
    assert far <= near


def test_taker_arm_fills_every_signal() -> None:
    report = execution.simulate(rising())
    assert report["taker_always_filled"]["episodes"] == report["signals"]


def test_missed_episodes_are_recorded_not_discarded() -> None:
    """The price of the cheaper fee is what it missed; dropping it would flatter maker."""
    report = execution.simulate(rising(spread=0.1), offset=0.03)
    assert report["missed_by_resting"]["episodes"] > 0


def test_negative_offset_is_rejected() -> None:
    with pytest.raises(ValueError, match="offset must be"):
        execution.simulate(rising(), offset=-0.01)


def test_absurd_offset_is_rejected() -> None:
    with pytest.raises(ValueError, match="offset must be"):
        execution.simulate(rising(), offset=0.5)


def test_simulation_is_deterministic() -> None:
    assert execution.simulate(rising()) == execution.simulate(rising())


def test_combine_weights_by_episode_count() -> None:
    reports = [execution.simulate(rising()), execution.simulate(rising(150))]
    pooled = execution.combine(reports)
    assert pooled["symbols"] == 2
    assert pooled["taker_always_filled"]["episodes"] == sum(
        r["taker_always_filled"]["episodes"] for r in reports
    )
