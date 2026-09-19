from __future__ import annotations

import pytest

from tools import exit_policy_sweep as sweep

SHIPPED = sweep.CANDIDATES[0]
WIDE = sweep.CANDIDATES[-1]


def test_shipped_candidate_matches_the_shipped_policy() -> None:
    """The comparison is worthless if its reference drifts from what actually ships."""
    from app.strategy.position_management import PositionManagementPolicy

    policy = PositionManagementPolicy()
    assert sweep._d(SHIPPED.stop) == policy.stop_loss_fraction
    assert sweep._d(SHIPPED.target) == policy.take_profit_fraction
    assert sweep._d(SHIPPED.trailing_activation) == policy.trailing_activation_fraction
    assert sweep._d(SHIPPED.trailing) == policy.trailing_stop_fraction
    assert SHIPPED.bars == policy.maximum_holding_bars


def test_driftless_result_is_close_to_the_round_trip_cost() -> None:
    """With no drift a sound policy gives back the cost and little else.

    A geometry that loses materially more than the round trip under a martingale is
    taking something from every trade before the entry has said anything.
    """
    result = sweep.measure(SHIPPED, drift=0.0, episodes=4000)
    assert -3 * sweep.DEFAULT_COST_FRACTION < result["mean_return"] < 0


def test_drift_is_converted_into_return() -> None:
    flat = sweep.measure(SHIPPED, drift=0.0, episodes=4000)
    moving = sweep.measure(SHIPPED, drift=0.002, episodes=4000)
    assert moving["mean_return"] > flat["mean_return"]


def test_a_wider_longer_geometry_converts_the_same_drift_better() -> None:
    """The finding this tool exists to record: geometry decides how much drift lands."""
    shipped = sweep.measure(SHIPPED, drift=0.002, episodes=4000)
    wide = sweep.measure(WIDE, drift=0.002, episodes=4000)
    assert wide["mean_return"] > shipped["mean_return"]


def test_no_geometry_turns_a_martingale_into_profit() -> None:
    """Any candidate showing positive return without drift would mean a broken model."""
    for candidate in sweep.CANDIDATES:
        result = sweep.measure(candidate, drift=0.0, episodes=2000)
        assert result["mean_return"] < 0, candidate.name


def test_measurement_is_deterministic() -> None:
    first = sweep.measure(SHIPPED, drift=0.002, episodes=2000)
    second = sweep.measure(SHIPPED, drift=0.002, episodes=2000)
    assert first == second


def test_impossible_volatility_is_rejected() -> None:
    with pytest.raises(ValueError, match="bar_sigma"):
        sweep.measure(SHIPPED, drift=0.0, bar_sigma=1.5, episodes=100)


def test_sweep_scores_every_candidate() -> None:
    rows = sweep.sweep(episodes=1200)
    assert len(rows) == len(sweep.CANDIDATES)
    assert all("driftless_return" in row and "drifted_return" in row for row in rows)


def test_cli_runs() -> None:
    assert sweep.main(["--episodes", "1200"]) == 0
