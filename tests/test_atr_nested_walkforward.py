"""Walk-forward selection safety: no current-bar, future fold or auto-promotion."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.position_management import PositionManagementPolicy
from tools.atr_entry_frozen_lab import ATR_ARMS
from tools.atr_nested_walkforward import (
    INNER_VALIDATION,
    OUTER_TEST,
    OUTER_TRAIN,
    frozen_atr_resolver,
    run_nested_atr,
    select_past_only_profiles,
)
from tools.historical_portfolio_walk_forward import folds
from tools.per_asset_exit_policy_matrix import SIX

D = Decimal
BARS = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
FUNDING = Path("research/snapshots/bybit_funding_2023-10-09_2026-10-08")


def _arms():
    return [
        {
            "symbol": symbol,
            "arm": arm,
            "return_with_funding_lower": "-0.02",
            "max_drawdown_fraction_before_funding": "0.10",
            "symbol_closed_trades": 5,
            "exit_path_changed": True,
        }
        for symbol in SIX for arm in ATR_ARMS
    ]


def _control():
    return {
        "return_with_funding_lower": "-0.03",
        "max_drawdown_fraction_before_funding": "0.10",
    }


def _completed(symbol):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return tuple(
        OhlcvBar(
            symbol=symbol, timestamp=start + timedelta(days=i),
            open=D("100"), high=D("101"), low=D("99"),
            close=D("100"), volume=1000, trade_count=100,
        )
        for i in range(15)
    )


def test_exact_two_profiles_for_every_coin_and_reject_missing_duplicates() -> None:
    rows = _arms()
    assert len(rows) == 12
    choices, decisions = select_past_only_profiles(
        control=_control(), inspected_arms=rows,
    )
    assert choices == {symbol: "ATR_COMPACT" for symbol in SIX}
    assert len(decisions) == 12
    assert all(item["eligible"] for item in decisions)
    with pytest.raises(ValueError, match="duplicated preregistered"):
        select_past_only_profiles(
            control=_control(), inspected_arms=[*rows[:-1], rows[0]],
        )
    with pytest.raises(ValueError, match="frozen six"):
        select_past_only_profiles(
            control=_control(), inspected_arms=rows,
            symbols=("BTCUSDT", "ETHUSDT"),
        )


def test_fallback_on_insufficient_trades_or_insignificant_delta() -> None:
    rows = _arms()
    for item in rows:
        item["return_with_funding_lower"] = "-0.027"
    rows[0]["return_with_funding_lower"] = "0.30"
    rows[0]["symbol_closed_trades"] = 2
    rows[2]["return_with_funding_lower"] = "0.20"
    rows[2]["max_drawdown_fraction_before_funding"] = "0.50"
    chosen, decisions = select_past_only_profiles(
        control=_control(), inspected_arms=rows,
    )
    assert all(name == "BASELINE" for name in chosen.values())
    assert any(not item["eligible"] for item in decisions)


def test_nested_splits_cannot_overlap_outer_tests() -> None:
    planned = folds(1088, train_bars=OUTER_TRAIN, test_bars=OUTER_TEST)
    assert OUTER_TRAIN == 420
    assert INNER_VALIDATION == OUTER_TEST == 90
    assert len(planned) == 7
    for index, fold in enumerate(planned):
        assert fold.context_start + OUTER_TRAIN == fold.execution_start
        assert fold.execution_start < fold.execution_end
        if index:
            assert planned[index - 1].execution_end == fold.execution_start


def test_profiles_frozen_and_not_accidentally_applied_to_other_symbols() -> None:
    profiles = dict.fromkeys(SIX, "BASELINE")
    profiles["BTCUSDT"] = "ATR_EXTENDED"
    audit = []
    resolver = frozen_atr_resolver(profiles, audit=audit)
    values = resolver("BTCUSDT", _completed("BTCUSDT"))
    assert values.stop_loss_fraction == D("0.015")
    assert values.take_profit_fraction == D("0.03")
    assert resolver("ETHUSDT", _completed("ETHUSDT")) == PositionManagementPolicy()
    profiles["BTCUSDT"] = "BASELINE"
    assert resolver("BTCUSDT", _completed("BTCUSDT")) == values
    assert len(audit) == 3
    assert all(x["frozen_at_entry"] for x in audit)
    with pytest.raises(ValueError, match="exact six-major"):
        frozen_atr_resolver({"BTCUSDT": "ATR_COMPACT"})
    with pytest.raises(ValueError, match="unknown ATR profile"):
        frozen_atr_resolver(dict.fromkeys(SIX, "EXPERIMENTAL_2026"))


def test_one_real_nested_fold_is_past_only_and_never_grants_live() -> None:
    report = run_nested_atr(
        bars_dir=BARS,
        funding_dir=FUNDING,
        max_folds=1,
    )
    assert report["evaluation_class"] == (
        "REUSED_INSPECTED_HISTORICAL_NESTED_DEVELOPMENT_ONLY"
    )
    assert report["research_only"] is True
    assert report["strategy_promotion_allowed"] is False
    assert report["live_trading_allowed"] is False
    assert report["untouched_holdout"] is False
    assert report["summary"]["outer_folds"] == 1
    assert report["summary"]["inner_predeclared_arms_evaluated"] == 12
    assert len(report["sources_sha256"]) == 12
    fold = report["folds"][0]
    inner = fold["inner_selection"]
    outer = fold["outer_test"]
    assert inner["last_available_decision_bar"] < outer["first_execution"]
    assert len(inner["all_predeclared_12_arms"]) == 12
    assert len(inner["all_selection_reasons"]) == 12
    assert set(inner["chosen_profiles_frozen_before_outer_test"]) == set(SIX)
    assert D(outer["selected_adaptive_portfolio"]["return_with_funding_lower"]) <= D(
        outer["selected_adaptive_portfolio"]["return_with_funding_upper"]
    )
    assert len(report["evidence_sha256"]) == 64
    assert all(x["frozen_at_entry"] for x in outer["entry_policy_audit"])


def test_timeline_and_empty_parameters_fail_closed() -> None:
    with pytest.raises(ValueError, match="insufficient chronology"):
        run_nested_atr(
            bars_dir=BARS,
            funding_dir=FUNDING,
            outer_train=80,
        )
    with pytest.raises(ValueError, match="insufficient prior inner"):
        run_nested_atr(
            bars_dir=BARS,
            funding_dir=FUNDING,
            outer_train=180,
            inner_validation=100,
        )
    with pytest.raises(ValueError, match="max_folds must be positive"):
        run_nested_atr(
            bars_dir=BARS,
            funding_dir=FUNDING,
            max_folds=0,
        )
