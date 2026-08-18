from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.qualify_bybit_crypto_directional_gate_v1 import (
    acquire_and_run_directional_gate,
    evaluate_directional_gate_decision,
    load_directional_gate_policy,
    prospective_readiness,
    prospective_validation_dates,
)

_POLICY = Path("research/bybit_crypto_directional_gate_v1_policy.json")


def _source_decision() -> dict[str, Any]:
    return {
        "research_stability_pass": True,
        "total_net_pnl_usdt": 20.0,
        "aggregate_profit_factor": 1.3,
        "worst_fold_drawdown_pct": 4.0,
    }


def _candidate_decision() -> dict[str, Any]:
    return {
        "research_stability_pass": True,
        "total_net_pnl_usdt": 30.0,
        "aggregate_profit_factor": 1.5,
        "worst_fold_drawdown_pct": 3.0,
    }


def _source_sides() -> dict[str, Any]:
    return {
        "SHORT": {
            "folds_with_trades": 4,
            "positive_net_pnl_fold_fraction_among_active_folds": 0.25,
            "total_net_pnl_usdt": -20.0,
            "aggregate_profit_factor": 0.5,
            "closed_trade_count": 12,
        }
    }


def _candidate_sides() -> dict[str, Any]:
    return {
        "SHORT": {
            "folds_with_trades": 0,
            "positive_net_pnl_fold_fraction_among_active_folds": None,
            "total_net_pnl_usdt": 0.0,
            "aggregate_profit_factor": None,
            "closed_trade_count": 0,
        }
    }


def test_prospective_window_is_fixed_and_not_ready_early() -> None:
    policy = load_directional_gate_policy(_POLICY)
    dates = prospective_validation_dates(policy)
    assert len(dates) == 28
    assert dates[0].isoformat() == "2026-08-17"
    assert dates[-1].isoformat() == "2026-09-13"

    early = prospective_readiness(
        now=datetime(2026, 8, 18, 12, tzinfo=UTC),
        policy=policy,
    )
    assert early["ready"] is False
    assert early["available_completed_utc_days"] == 1
    assert early["qualification"] == "HOLD_INSUFFICIENT_PROSPECTIVE_DAYS"

    ready = prospective_readiness(
        now=datetime(2026, 9, 14, 0, 1, tzinfo=UTC),
        policy=policy,
    )
    assert ready["ready"] is True
    assert ready["available_completed_utc_days"] == 28


def test_pre_window_hold_does_not_touch_market_client() -> None:
    class ExplodingClient:
        def fetch_klines(self, **_: object) -> object:
            raise AssertionError("market acquisition must not run before fixed window completes")

    report = acquire_and_run_directional_gate(
        now=datetime(2026, 8, 18, 12, tzinfo=UTC),
        policy_path=_POLICY,
        client=ExplodingClient(),  # type: ignore[arg-type]
    )
    assert report["qualification"] == "HOLD_INSUFFICIENT_PROSPECTIVE_DAYS"
    assert report["source"] == "NO_MARKET_ACQUISITION_BEFORE_FIXED_WINDOW_IS_COMPLETE"
    assert report["automatic_strategy_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False


def test_directional_gate_requires_persistent_source_short_weakness_and_candidate_quality() -> None:
    policy = load_directional_gate_policy(_POLICY)
    decision = evaluate_directional_gate_decision(
        policy=policy,
        source_decision=_source_decision(),
        source_side_diagnostics=_source_sides(),
        candidate_decision=_candidate_decision(),
        candidate_side_diagnostics=_candidate_sides(),
    )
    assert decision["prospective_research_pass"] is True
    assert decision["reasons"] == []
    assert decision["automatic_strategy_activation_allowed"] is False
    assert decision["strategy_promotion_allowed"] is False


def test_directional_gate_rejects_nonpersistent_short_weakness() -> None:
    policy = load_directional_gate_policy(_POLICY)
    sides = _source_sides()
    sides["SHORT"] = {
        **sides["SHORT"],
        "positive_net_pnl_fold_fraction_among_active_folds": 0.5,
    }
    decision = evaluate_directional_gate_decision(
        policy=policy,
        source_decision=_source_decision(),
        source_side_diagnostics=sides,
        candidate_decision=_candidate_decision(),
        candidate_side_diagnostics=_candidate_sides(),
    )
    assert decision["prospective_research_pass"] is False
    assert "SOURCE_SHORT_NOT_PERSISTENTLY_WEAK_BY_FOLD" in decision["reasons"]


def test_directional_gate_rejects_candidate_that_is_worse_than_source() -> None:
    policy = load_directional_gate_policy(_POLICY)
    candidate = {
        **_candidate_decision(),
        "total_net_pnl_usdt": 19.0,
        "aggregate_profit_factor": 1.2,
        "worst_fold_drawdown_pct": 4.5,
    }
    decision = evaluate_directional_gate_decision(
        policy=policy,
        source_decision=_source_decision(),
        source_side_diagnostics=_source_sides(),
        candidate_decision=candidate,
        candidate_side_diagnostics=_candidate_sides(),
    )
    assert decision["prospective_research_pass"] is False
    assert "LONG_ONLY_NET_PNL_WORSE_THAN_SOURCE" in decision["reasons"]
    assert "LONG_ONLY_PROFIT_FACTOR_WORSE_THAN_SOURCE" in decision["reasons"]
    assert "LONG_ONLY_DRAWDOWN_WORSE_THAN_SOURCE" in decision["reasons"]
