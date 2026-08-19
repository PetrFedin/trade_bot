from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tools.qualify_strategy_quality_shadow import qualify

MANIFEST = Path("data/historical/aapl_plotly_multiregime/manifest.json")
POLICY = Path(
    "data/historical/aapl_plotly_multiregime/qualification_strategy_quality_shadow.json"
)


def test_strategy_quality_shadow_is_measured_but_never_promoted() -> None:
    evidence = qualify(MANIFEST, POLICY)
    assert evidence["shadow_only"] is True
    assert evidence["promotion_allowed"] is False
    assert evidence["source_classification"] == "THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE"
    blockers = set(evidence["promotion_blockers"])
    assert "SHADOW_ONLY_POLICY" in blockers
    assert "SINGLE_SYMBOL_HISTORICAL_EVIDENCE" in blockers
    assert "NO_EXTERNAL_PAPER_STRATEGY_EVIDENCE" in blockers


def test_shadow_tracks_trade_quality_not_just_profitable_trade_rate() -> None:
    evidence = qualify(MANIFEST, POLICY)
    aggregate = evidence["aggregate"]
    assert aggregate["closed_trade_count"] == 3
    assert aggregate["winning_trades"] == 2
    assert aggregate["losing_trades"] == 1
    assert Decimal(aggregate["win_rate"]) == Decimal(2) / Decimal(3)
    assert Decimal(aggregate["profit_factor"]) < Decimal("1")
    assert Decimal(aggregate["net_closed_trade_pnl"]) < Decimal("0")
    assert aggregate["positive_mfe_trades"] >= aggregate["positive_mfe_closed_profitable"]
    assert aggregate["profit_preservation_rate"] is not None
    assert aggregate["average_mfe_capture_ratio"] is not None
    assert "AGGREGATE_PROFIT_FACTOR_BELOW_ONE" in evidence["promotion_blockers"]
    assert "AGGREGATE_CLOSED_TRADE_PNL_NOT_POSITIVE" in evidence["promotion_blockers"]


def test_regime_evidence_retains_mfe_mae_and_capture_for_each_trade() -> None:
    evidence = qualify(MANIFEST, POLICY)
    trades = [
        trade
        for regime in evidence["regimes"]
        for trade in regime["candidate"]["closed_trades"]
    ]
    assert trades
    for trade in trades:
        assert "maximum_favorable_excursion_fraction" in trade
        assert "maximum_adverse_excursion_fraction" in trade
        assert "mfe_capture_ratio" in trade
        assert "mfe_giveback_fraction" in trade


def test_shadow_candidate_improves_downside_without_claiming_profitability() -> None:
    evidence = qualify(MANIFEST, POLICY)
    aggregate = evidence["aggregate"]
    assert aggregate["candidate_mean_return_not_worse"] is True
    assert aggregate["candidate_worst_drawdown_not_worse"] is True
    assert Decimal(aggregate["mean_candidate_return"]) < Decimal("0")
    assert Decimal(aggregate["mean_candidate_return"]) > Decimal(
        aggregate["mean_control_return"]
    )
    assert Decimal(aggregate["worst_candidate_drawdown_fraction"]) < Decimal(
        aggregate["worst_control_drawdown_fraction"]
    )
