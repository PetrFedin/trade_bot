from __future__ import annotations

import pytest

from tools import exit_geometry_search as search


def test_shipped_geometry_is_in_the_grid() -> None:
    """A search that cannot compare against what it replaces has found nothing."""
    assert search.SHIPPED in search.grid()


def test_shipped_geometry_matches_the_shipped_policy() -> None:
    from app.strategy.position_management import PositionManagementPolicy

    shipped = PositionManagementPolicy()
    built = search.SHIPPED.policy()
    assert built.stop_loss_fraction == shipped.stop_loss_fraction
    assert built.take_profit_fraction == shipped.take_profit_fraction
    assert built.trailing_activation_fraction == shipped.trailing_activation_fraction
    assert built.trailing_stop_fraction == shipped.trailing_stop_fraction
    assert built.maximum_holding_bars == shipped.maximum_holding_bars


def test_grid_entries_are_distinct() -> None:
    items = search.grid()
    assert len(items) == len(set(items))


def test_every_geometry_builds_a_valid_policy() -> None:
    for geometry in search.grid():
        geometry.policy().validate()


def test_holm_controls_the_family_wise_error_rate() -> None:
    """Testing many geometries inflates the best one; Holm is what pays for that."""
    adjusted = search._holm([("a", 0.01), ("b", 0.02), ("c", 0.6)])
    assert adjusted["a"] == pytest.approx(0.03)
    assert adjusted["b"] == pytest.approx(0.04)
    assert adjusted["c"] == pytest.approx(0.6)


def test_holm_never_reports_below_the_raw_value() -> None:
    raw = [("a", 0.004), ("b", 0.2), ("c", 0.9)]
    adjusted = search._holm(raw)
    for name, value in raw:
        assert adjusted[name] >= value


def test_holm_is_monotone_in_rank() -> None:
    adjusted = search._holm([("a", 0.001), ("b", 0.01), ("c", 0.05), ("d", 0.4)])
    values = [adjusted[name] for name in ("a", "b", "c", "d")]
    assert values == sorted(values)


def test_holm_caps_at_one() -> None:
    adjusted = search._holm([("a", 0.9), ("b", 0.95)])
    assert all(value <= 1.0 for value in adjusted.values())


def test_bootstrap_interval_brackets_the_mean() -> None:
    values = [0.01, 0.012, 0.009, 0.011, 0.013, 0.008, 0.010]
    low, high = search._bootstrap(values, draws=800)
    assert low is not None and high is not None
    assert low < sum(values) / len(values) < high


def test_bootstrap_declines_on_a_tiny_sample() -> None:
    assert search._bootstrap([0.01, 0.02]) == (None, None)


def test_bootstrap_is_reproducible() -> None:
    values = [0.01, 0.012, 0.009, 0.011, 0.013, 0.008]
    assert search._bootstrap(values, draws=500) == search._bootstrap(values, draws=500)


def test_degenerate_split_is_rejected() -> None:
    with pytest.raises(ValueError, match="development_fraction"):
        search.search([], development_fraction=0.99)


def test_normal_survival_function_matches_known_points() -> None:
    assert search._normal_sf(0.0) == pytest.approx(0.5)
    assert search._normal_sf(1.96) == pytest.approx(0.025, abs=0.001)
    assert search._normal_sf(2.58) == pytest.approx(0.0049, abs=0.001)
