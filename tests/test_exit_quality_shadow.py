from datetime import datetime
from decimal import Decimal
from pathlib import Path

from tools.qualify_exit_quality_shadow import qualify

MANIFEST = Path("data/historical/aapl_plotly_multiregime/manifest.json")
POLICY = Path(
    "data/historical/aapl_plotly_multiregime/qualification_strategy_quality_shadow.json"
)


def test_exit_quality_shadow_is_auditable_and_non_promotable() -> None:
    evidence = qualify(MANIFEST, POLICY)
    assert evidence["schema_version"] == "exit-quality-shadow-v1"
    assert evidence["shadow_only"] is True
    assert evidence["promotion_allowed"] is False
    assert evidence["source_classification"] == "THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE"
    assert set(evidence["limitations"]) == {
        "CLOSE_ONLY_EXCURSION_MEASUREMENT",
        "THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE",
        "SINGLE_SYMBOL_HISTORICAL_EVIDENCE",
        "NO_EXTERNAL_PAPER_STRATEGY_EVIDENCE",
    }


def test_exit_quality_shadow_tracks_every_holdout_decision() -> None:
    evidence = qualify(MANIFEST, POLICY)
    aggregate = evidence["aggregate"]
    assert aggregate["decision_count"] == 30
    assert sum(aggregate["decision_action_counts"].values()) == 30
    assert aggregate["decision_action_counts"] == {
        "ENTER": 3,
        "EXIT": 3,
        "HOLD": 5,
        "STAY_FLAT": 19,
    }
    assert aggregate["closed_trade_count"] == 3
    assert aggregate["positive_mfe_trades"] >= aggregate["winning_trades"]

    for regime in evidence["regimes"]:
        assert len(regime["decision_trace"]) == regime["holdout_bars"] == 10
        for item in regime["decision_trace"]:
            assert datetime.fromisoformat(item["decision_time"]) < datetime.fromisoformat(
                item["execution_time"]
            )
            if item["action"] == "EXIT":
                assert item["exit_reason"] is not None


def test_sample_exposes_signal_exit_dominance_and_partial_profit_giveback() -> None:
    evidence = qualify(MANIFEST, POLICY)
    aggregate = evidence["aggregate"]
    assert aggregate["exit_reason_counts"] == {"SIGNAL_EXIT": 3}
    assert aggregate["signal_exit_count"] == 3
    assert aggregate["managed_exit_count"] == 0
    assert aggregate["position_manager_exit_overrides"] == 0
    assert aggregate["trades_with_mfe_giveback"] == 2
    assert aggregate["profit_giveback_observed"] is True
    assert aggregate["zero_mfe_losing_trades"] == 1
    assert Decimal(aggregate["average_mfe_capture_ratio"]) > Decimal("0.49")
    assert Decimal(aggregate["average_mfe_capture_ratio"]) < Decimal("0.53")
    assert set(evidence["observed_findings"]) == {
        "POSITION_MANAGER_NOT_TRIGGERED_IN_SAMPLE",
        "PROFIT_GIVEBACK_OBSERVED",
        "LOSING_TRADE_WITH_ZERO_POSITIVE_MFE",
    }


def test_closed_trade_excursion_metrics_are_non_negative_and_consistent() -> None:
    evidence = qualify(MANIFEST, POLICY)
    trades = [
        trade
        for regime in evidence["regimes"]
        for trade in regime["closed_trades"]
    ]
    assert len(trades) == evidence["aggregate"]["closed_trade_count"]
    for trade in trades:
        assert Decimal(trade["maximum_favorable_excursion_fraction"]) >= 0
        assert Decimal(trade["maximum_adverse_excursion_fraction"]) >= 0
        if trade["mfe_capture_ratio"] is not None:
            assert trade["mfe_giveback_fraction"] is not None
            assert Decimal(trade["mfe_capture_ratio"]) + Decimal(
                trade["mfe_giveback_fraction"]
            ) == Decimal("1")
