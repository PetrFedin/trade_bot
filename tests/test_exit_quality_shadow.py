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
    assert aggregate["closed_trade_count"] == 3
    assert aggregate["positive_mfe_trades"] >= aggregate["winning_trades"]
    assert isinstance(aggregate["profit_giveback_observed"], bool)

    for regime in evidence["regimes"]:
        assert len(regime["decision_trace"]) == regime["holdout_bars"] == 10
        for item in regime["decision_trace"]:
            assert datetime.fromisoformat(item["decision_time"]) < datetime.fromisoformat(
                item["execution_time"]
            )
            if item["action"] == "EXIT":
                assert item["exit_reason"] is not None


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
