"""Research-only heterogeneous crypto signal experiments."""
from decimal import Decimal
from pathlib import Path

import pytest

from app.strategy.regime_momentum import RegimeAwareMomentumConfig
from tools.per_asset_conditions_lab import (
    SIX, SingleAssetResearchSelector, _signal_variant, run_experiments,
)

BASE = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")


def test_variants_are_distinct_and_have_valid_signal_contracts() -> None:
    base = RegimeAwareMomentumConfig()
    strict = _signal_variant("strict")
    relaxed = _signal_variant("relaxed")
    for cfg in (base, strict, relaxed):
        cfg.validate()
        assert cfg.minimum_history_bars == 8
    assert strict.minimum_momentum_return > base.minimum_momentum_return
    assert strict.maximum_realized_volatility < base.maximum_realized_volatility
    assert relaxed.maximum_realized_volatility > base.maximum_realized_volatility
    with pytest.raises(ValueError, match="unknown preregistered"):
        _signal_variant("optimized_2024")


def test_research_selector_cannot_accept_unknown_variant() -> None:
    # Constructing a selector is harmless; applying invalid variant fails closed.
    selector = SingleAssetResearchSelector(
        experiment_symbol="BTCUSDT", variant="invalid")
    assert selector.experiment_symbol == "BTCUSDT"
    with pytest.raises(ValueError):
        _signal_variant(selector.variant)


def test_all_12_arms_conserve_research_only_identity() -> None:
    report = run_experiments(bars_dir=BASE)
    assert len(report["experiments"]) == 2 * len(SIX)
    assert {x["symbol"] for x in report["experiments"]} == set(SIX)
    assert all(x["winner_selected"] is False for x in report["experiments"])
    assert report["strategy_promotion_allowed"] is False
    assert report["live_trading_allowed"] is False
    assert report["untouched_holdout"] is False
    assert report["funding_modelled"] is False
    assert len(report["source_sha256"]) == 6
    assert len(report["evidence_sha256"]) == 64
    assert sum(x["closed_trades"] >= 0 for x in report["experiments"]) == 12
    baseline = Decimal(report["control"]["return_before_funding"])
    for row in report["experiments"]:
        assert Decimal(row["delta_vs_control_before_funding"]) == (
            Decimal(row["return_before_funding"]) - baseline
        )


def test_replay_is_deterministic() -> None:
    a = run_experiments(bars_dir=BASE)
    b = run_experiments(bars_dir=BASE)
    assert a == b
    assert a["evidence_sha256"] == b["evidence_sha256"]


def test_no_universe_selection_on_already_inspected_history() -> None:
    with pytest.raises(ValueError, match="fixed to six"):
        run_experiments(bars_dir=BASE, symbols=("BTCUSDT", "ETHUSDT"))
