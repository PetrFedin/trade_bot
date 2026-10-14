"""Nested Candidate B development: rejection of temporal leakage and winners."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.ohlcv import OhlcvBar
from tools.entry_impulse_exhaustion_lab import HYPOTHESES, SIX
from tools.entry_impulse_nested_walkforward import (
    INNER_CHECK,
    LATER_CHECK,
    PAST_CONTEXT,
    freeze_past_impulse_map,
    run_nested_impulse,
    select_profiles_using_inner_only,
)
from tools.historical_portfolio_walk_forward import folds

D = Decimal
BARS = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
FUNDING = Path("research/snapshots/bybit_funding_2023-10-09_2026-10-08")


def _inner_rows():
    return [
        {
            "symbol": symbol,
            "hypothesis": hypothesis,
            "vetoed_entries": 2,
            "target_symbol_closed_trades": 3,
            "closed_trades": 12,
            "return_funding_lower": "0.02",
            "max_drawdown_fraction": "0.08",
        }
        for symbol in SIX for hypothesis in HYPOTHESES
    ]


def _inner_control():
    return {"return_funding_lower": "0.0", "max_drawdown_fraction": "0.075"}


def _historical(symbol):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return tuple(
        OhlcvBar(
            symbol=symbol, timestamp=start + timedelta(days=i),
            open=D("100"), high=D("101"), low=D("99"),
            close=D("100"), volume=1000, trade_count=100,
        )
        for i in range(16)
    )


def test_fixed_12_arm_past_selection_deterministically_ties() -> None:
    chosen, decision_log = select_profiles_using_inner_only(
        _inner_control(), _inner_rows(),
    )
    assert len(decision_log) == 12
    assert all(x["eligible"] for x in decision_log)
    assert chosen == {sym: "PRIOR_1D_ATR_150" for sym in SIX}


def test_missing_trials_and_evidence_block_past_selection() -> None:
    with pytest.raises(ValueError, match="missing or duplicated"):
        select_profiles_using_inner_only(
            _inner_control(), _inner_rows()[:-1],
        )
    with pytest.raises(ValueError, match="missing or duplicated"):
        select_profiles_using_inner_only(
            _inner_control(), [*_inner_rows()[:-1], _inner_rows()[0]],
        )
    rows = _inner_rows()
    for row in rows:
        row["return_funding_lower"] = "0.001"
    rows[0]["return_funding_lower"] = "0.80"
    rows[0]["closed_trades"] = 0
    rows[2]["return_funding_lower"] = "0.90"
    rows[2]["max_drawdown_fraction"] = "0.20"
    selected, rejected = select_profiles_using_inner_only(
        _inner_control(), rows,
    )
    assert all(profile == "BASELINE" for profile in selected.values())
    assert any(not x["eligible"] for x in rejected)


def test_frozen_map_never_mutates_with_caller_or_uses_wrong_coin() -> None:
    plan = dict.fromkeys(SIX, "BASELINE")
    plan["BTCUSDT"] = "PRIOR_1D_ATR_150"
    audit = []
    callback = freeze_past_impulse_map(plan, audit=audit)
    plan["BTCUSDT"] = "BASELINE"
    assert callback("BTCUSDT", _historical("BTCUSDT")) is True
    assert callback("ETHUSDT", _historical("ETHUSDT")) is True
    assert len(audit) == 1
    assert audit[0]["hypothesis"] == "PRIOR_1D_ATR_150"
    with pytest.raises(ValueError, match="incomplete symbols"):
        freeze_past_impulse_map({"BTCUSDT": "BASELINE"})
    with pytest.raises(ValueError, match="non-preregistered"):
        freeze_past_impulse_map(dict.fromkeys(SIX, "FITTED_ON_OUTER"))


def test_original_nested_training_never_overlaps_later_windows() -> None:
    planned = folds(1088, train_bars=PAST_CONTEXT, test_bars=LATER_CHECK)
    assert PAST_CONTEXT == 420
    assert INNER_CHECK == LATER_CHECK == 90
    assert len(planned) == 7
    for i, fold in enumerate(planned):
        assert fold.context_start + PAST_CONTEXT == fold.execution_start
        if i:
            assert planned[i - 1].execution_end == fold.execution_start


def test_one_actual_prior_only_fold_preserves_negative_evidence() -> None:
    report = run_nested_impulse(
        bars_dir=BARS,
        funding_dir=FUNDING,
        max_folds=1,
    )
    assert report["schema_version"] == (
        "astra-candidate-b-nested-entry-veto-development-v1"
    )
    assert report["summary"]["outer_test_folds"] == 1
    assert report["summary"]["inner_candidate_portfolios"] == 12
    assert report["strategy_promotion_allowed"] is False
    assert report["untouched_holdout"] is False
    assert report["live_trading_allowed"] is False
    assert report["outer_test_used_to_choose_profiles"] is False
    assert report["original_exit_policy_unchanged"] is True
    assert len(report["source_sha256"]) == 12
    assert len(report["evidence_sha256"]) == 64
    fold = report["folds"][0]
    inner = fold["inner_training"]
    outer = fold["outer_test"]
    assert inner["last_known_prior_bar"] < outer["first_execution_bar"]
    assert len(inner["all_12_single_symbol_arms"]) == 12
    assert len(inner["all_rejections_and_qualifications"]) == 12
    assert set(inner["selected_profile_map_before_outer_test"]) == set(SIX)
    assert outer["stress_10bps_fee_10bps_slippage_pre_funding"][
        "funding_included"
    ] is False
    assert D(outer["prior_picked_joint_entry_policy"]["return_funding_lower"]) <= D(
        outer["prior_picked_joint_entry_policy"]["return_funding_upper"]
    )


def test_bad_windows_fail_closed_before_any_economics() -> None:
    with pytest.raises(ValueError, match="insufficient source"):
        run_nested_impulse(
            bars_dir=BARS,
            funding_dir=FUNDING,
            outer_train=90,
        )
    with pytest.raises(ValueError, match="insufficient prior warmup"):
        run_nested_impulse(
            bars_dir=BARS,
            funding_dir=FUNDING,
            outer_train=180,
            inner_test=100,
        )
    with pytest.raises(ValueError, match="max_folds must be positive"):
        run_nested_impulse(
            bars_dir=BARS,
            funding_dir=FUNDING,
            max_folds=0,
        )
