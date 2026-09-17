from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tools import strategy_diagnostics as diagnostics

GEOMETRY = diagnostics.Geometry(
    stop_fraction=Decimal("0.02"), target_fraction=Decimal("0.04")
)
NO_COST = Decimal("0")


def evidence(target_first: int, stop_first: int) -> dict:
    return {"target_first": target_first, "stop_first": stop_first}


def test_baseline_is_the_geometry_ratio() -> None:
    """A 2:1 geometry gives a driftless walk one target-first episode in three."""
    assert GEOMETRY.baseline_target_first == pytest.approx(1 / 3)
    assert GEOMETRY.reward_to_risk == pytest.approx(2.0)


def test_baseline_geometry_is_a_coin_flip_before_costs() -> None:
    at_baseline = diagnostics.expectancy_per_unit_risk(1 / 3, GEOMETRY, NO_COST)
    assert at_baseline == pytest.approx(0.0, abs=1e-9)


def test_costs_are_charged_in_units_of_the_stop_distance() -> None:
    """A 0.16% round trip consumes 0.08 of a 2% stop, on every episode."""
    free = diagnostics.expectancy_per_unit_risk(1 / 3, GEOMETRY, NO_COST)
    charged = diagnostics.expectancy_per_unit_risk(1 / 3, GEOMETRY, Decimal("0.0016"))
    assert free - charged == pytest.approx(0.08)


def test_geometry_comes_from_the_live_policy() -> None:
    """The gate must read the shipped policy, never a copy that can drift from it."""
    from app.strategy.position_management import PositionManagementPolicy

    policy = PositionManagementPolicy()
    geometry = diagnostics.policy_geometry()
    assert geometry.stop_fraction == policy.stop_loss_fraction
    assert geometry.target_fraction == policy.take_profit_fraction


def test_frozen_negative_record_is_worse_than_random() -> None:
    """The committed evidence must keep failing: 137/608 against a 33.3% baseline."""
    report = diagnostics.diagnose(evidence(137, 471), GEOMETRY)
    assert report["verdict"] == diagnostics.Verdict.WORSE_THAN_RANDOM
    assert report["promotion_allowed"] is False
    assert report["significance"]["p_worse_than_baseline"] < 1e-6
    assert report["expectancy_per_unit_risk"]["observed"] < 0


def test_a_result_at_baseline_is_not_an_edge() -> None:
    """Exactly coin-flip performance must not be promotable, however large the sample."""
    report = diagnostics.diagnose(evidence(200, 400), GEOMETRY)
    assert report["verdict"] == diagnostics.Verdict.INDISTINGUISHABLE
    assert report["promotion_allowed"] is False


def test_a_positive_but_sub_baseline_result_is_not_an_edge() -> None:
    """30% target-first turns a profit on paper yet still loses to a random entry."""
    report = diagnostics.diagnose(evidence(180, 420), GEOMETRY, cost_fraction=NO_COST)
    assert report["rates"]["observed_target_first"] == pytest.approx(0.30)
    assert report["verdict"] != diagnostics.Verdict.EDGE_PRESENT
    assert report["promotion_allowed"] is False


def test_a_real_edge_is_recognised() -> None:
    """A rate well past baseline and past costs must be promotable, or the gate is vacuous."""
    report = diagnostics.diagnose(evidence(280, 320), GEOMETRY)
    assert report["verdict"] == diagnostics.Verdict.EDGE_PRESENT
    assert report["promotion_allowed"] is True
    assert report["expectancy_per_unit_risk"]["observed"] > 0


def test_a_small_lucky_sample_is_not_an_edge() -> None:
    """Six of ten beats the baseline but proves nothing; the gate must hold."""
    report = diagnostics.diagnose(evidence(6, 4), GEOMETRY)
    assert report["promotion_allowed"] is False


def test_breakeven_rate_accounts_for_costs() -> None:
    free = diagnostics.diagnose(evidence(137, 471), GEOMETRY, cost_fraction=NO_COST)
    charged = diagnostics.diagnose(evidence(137, 471), GEOMETRY)
    assert free["rates"]["breakeven_target_first"] == pytest.approx(1 / 3)
    assert charged["rates"]["breakeven_target_first"] > free["rates"]["breakeven_target_first"]


def test_empty_evidence_is_rejected() -> None:
    with pytest.raises(SystemExit, match="EVIDENCE_EMPTY"):
        diagnostics.diagnose(evidence(0, 0), GEOMETRY)


def test_invalid_geometry_is_rejected() -> None:
    bad = diagnostics.Geometry(stop_fraction=Decimal("0"), target_fraction=Decimal("0.04"))
    with pytest.raises(ValueError, match="stop_fraction"):
        diagnostics.diagnose(evidence(1, 1), bad)


def test_committed_evidence_fails_the_gate(tmp_path: Path) -> None:
    """Running the gate over the repository's own evidence must exit non-zero today."""
    assert diagnostics.main(["--require-edge"]) == 1


def test_gate_passes_when_evidence_shows_an_edge(tmp_path: Path) -> None:
    path = tmp_path / "edge.json"
    path.write_text(json.dumps(evidence(280, 320)))
    assert diagnostics.main([str(path), "--require-edge"]) == 0


def test_incomplete_geometry_arguments_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "e.json"
    path.write_text(json.dumps(evidence(1, 1)))
    with pytest.raises(SystemExit, match="GEOMETRY_INCOMPLETE"):
        diagnostics.main([str(path), "--stop-fraction", "0.02"])


def test_overclaimed_promotion_is_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Declaring promotion without an edge is the failure this gate exists to stop."""
    path = tmp_path / "overclaim.json"
    path.write_text(json.dumps({**evidence(137, 471), "promotion_allowed": True}))
    assert diagnostics.main([str(path), "--enforce-declared-promotion"]) == 1
    assert "PROMOTION_OVERCLAIMED" in capsys.readouterr().err


def test_absent_edge_alone_does_not_fail_enforcement(tmp_path: Path) -> None:
    """A negative result that honestly declares itself negative must not break CI."""
    path = tmp_path / "honest.json"
    path.write_text(json.dumps({**evidence(137, 471), "promotion_allowed": False}))
    assert diagnostics.main([str(path), "--enforce-declared-promotion"]) == 0


def test_declared_promotion_with_a_real_edge_passes(tmp_path: Path) -> None:
    path = tmp_path / "earned.json"
    path.write_text(json.dumps({**evidence(280, 320), "promotion_allowed": True}))
    assert diagnostics.main([str(path), "--enforce-declared-promotion"]) == 0


def test_committed_evidence_does_not_overclaim() -> None:
    assert diagnostics.main(["--enforce-declared-promotion"]) == 0
