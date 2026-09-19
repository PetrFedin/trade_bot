from __future__ import annotations

import random

import pytest

from tools import predictability_audit as audit


def series(n: int, *, ar: float = 0.0, seed: int = 11) -> list[float]:
    rng = random.Random(seed)
    values = []
    previous = 0.0
    for _ in range(n):
        value = ar * previous + rng.gauss(0.0, 0.01)
        values.append(value)
        previous = value
    return values


def test_a_random_walk_is_not_mistaken_for_structure() -> None:
    """The whole test is worthless if noise reads as a regime."""
    for horizon in (2, 4, 8):
        result = audit.variance_ratio(series(20000), horizon)
        assert result["regime"] == "RANDOM_WALK"
        assert abs(result["variance_ratio"] - 1.0) < 0.05


def test_mean_reversion_is_detected() -> None:
    result = audit.variance_ratio(series(20000, ar=-0.35), 2)
    assert result["regime"] == "MEAN_REVERTING"
    assert result["variance_ratio"] < 0.8


def test_trending_is_detected() -> None:
    result = audit.variance_ratio(series(20000, ar=0.30), 2)
    assert result["regime"] == "TRENDING"
    assert result["variance_ratio"] > 1.2


def test_autocorrelation_recovers_a_known_coefficient() -> None:
    value = audit.autocorrelation(series(40000, ar=0.4), 1)
    assert 0.3 < value < 0.5


def test_autocorrelation_of_noise_is_near_zero() -> None:
    assert abs(audit.autocorrelation(series(40000), 1)) < 0.03


def test_short_series_is_rejected_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="need at least"):
        audit.variance_ratio(series(20), 8)


def test_degenerate_horizon_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        audit.variance_ratio(series(1000), 1)


def test_flat_series_is_rejected() -> None:
    with pytest.raises(ValueError, match="no variance"):
        audit.variance_ratio([0.0] * 1000, 2)


def test_log_returns_skip_non_positive_prices() -> None:
    assert audit.log_returns([100.0, 110.0, 121.0]) == pytest.approx(
        [0.0953102, 0.0953102], rel=1e-4
    )
