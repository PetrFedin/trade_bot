from __future__ import annotations

import pytest

from tools import cross_sectional_study as study


def obs(date: str, symbol: str, score: float, forward: float, eligible: bool = True):
    return study.Observation(
        date=date, symbol=symbol, score=score, forward_return=forward, eligible=eligible
    )


def panel(perfect: bool) -> list[study.Observation]:
    """Twelve symbols over forty dates; score either predicts the return or is noise."""
    rows = []
    for day in range(40):
        for index in range(12):
            score = index / 12
            forward = (index / 12 - 0.5) * 0.10 if perfect else ((day * 7 + index) % 5 - 2) * 0.01
            rows.append(obs(f"2026-01-{day + 1:02d}", f"S{index}", score, forward))
    return rows


def test_a_perfect_ranking_is_detected() -> None:
    """If a score that perfectly orders returns did not pay, the machinery is broken."""
    result = study.evaluate(panel(perfect=True), hold=1, fraction=0.25, cost=0.0)
    assert result["spread_long_short"]["mean"] > 0.05


def test_costs_are_charged_to_both_legs() -> None:
    free = study.evaluate(panel(perfect=True), hold=1, fraction=0.25, cost=0.0)
    charged = study.evaluate(panel(perfect=True), hold=1, fraction=0.25, cost=0.01)
    difference = free["spread_long_short"]["mean"] - charged["spread_long_short"]["mean"]
    assert difference == pytest.approx(0.02, abs=1e-6)


def test_a_useless_score_does_not_produce_a_spread() -> None:
    result = study.evaluate(panel(perfect=False), hold=1, fraction=0.25, cost=0.0)
    assert abs(result["spread_long_short"]["mean"]) < 0.02


def test_shuffled_control_is_reported() -> None:
    result = study.evaluate(panel(perfect=True), hold=1, fraction=0.25, cost=0.0)
    assert result["shuffled_ranking_control"]["periods"] > 0
    assert result["shuffled_ranking_control"]["mean"] is not None


def test_a_perfect_ranking_beats_its_own_shuffle() -> None:
    result = study.evaluate(panel(perfect=True), hold=1, fraction=0.25, cost=0.0)
    assert result["spread_long_short"]["mean"] > result["shuffled_ranking_control"]["mean"]


def test_rebalance_dates_do_not_overlap() -> None:
    dates = [f"d{index}" for index in range(30)]
    chosen = study._rebalance_dates(dates, 10)
    assert chosen == ["d0", "d10", "d20"]


def test_degenerate_leg_fraction_is_rejected() -> None:
    with pytest.raises(ValueError, match="fraction must be"):
        study.evaluate(panel(perfect=True), fraction=0.9)


def test_thin_dates_are_skipped_rather_than_traded() -> None:
    """A spread across three instruments is not a spread."""
    rows = [obs("2026-01-01", f"S{i}", i / 3, 0.01) for i in range(3)]
    result = study.evaluate(rows, hold=1, fraction=0.25, minimum_breadth=8)
    assert result["spread_long_short"]["periods"] == 0


def test_eligible_only_restricts_the_long_leg() -> None:
    """The gate carries the measured alpha, so the long leg must be able to honour it."""
    rows = []
    for day in range(30):
        for index in range(12):
            rows.append(
                obs(
                    f"2026-02-{day + 1:02d}",
                    f"S{index}",
                    index / 12,
                    0.05 if index < 6 else -0.05,
                    eligible=index < 6,
                )
            )
    gated = study.evaluate(rows, hold=1, fraction=0.25, cost=0.0, eligible_only=True)
    ungated = study.evaluate(rows, hold=1, fraction=0.25, cost=0.0)
    assert gated["long_only_top"]["mean"] > ungated["long_only_top"]["mean"]
