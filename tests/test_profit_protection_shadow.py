from decimal import Decimal
from pathlib import Path

from tools.qualify_strategy_quality_shadow import qualify

MANIFEST = Path("data/historical/aapl_plotly_multiregime/manifest.json")
POLICY = Path(
    "data/historical/aapl_plotly_multiregime/qualification_profit_protection_shadow.json"
)


def test_profit_protection_candidate_remains_shadow_only() -> None:
    evidence = qualify(MANIFEST, POLICY)
    assert evidence["shadow_only"] is True
    assert evidence["promotion_allowed"] is False
    blockers = set(evidence["promotion_blockers"])
    assert "SHADOW_ONLY_POLICY" in blockers
    assert "SINGLE_SYMBOL_HISTORICAL_EVIDENCE" in blockers
    assert "NO_EXTERNAL_PAPER_STRATEGY_EVIDENCE" in blockers


def test_profit_protection_candidate_exposes_preservation_and_capture_metrics() -> None:
    evidence = qualify(MANIFEST, POLICY)
    aggregate = evidence["aggregate"]
    assert aggregate["positive_mfe_trades"] >= 0
    preservation = aggregate["profit_preservation_rate"]
    if preservation is not None:
        assert Decimal("0") <= Decimal(preservation) <= Decimal("1")
    trades = [
        trade
        for regime in evidence["regimes"]
        for trade in regime["candidate"]["closed_trades"]
    ]
    for trade in trades:
        assert "maximum_favorable_excursion_fraction" in trade
        assert "maximum_adverse_excursion_fraction" in trade
        assert "mfe_capture_ratio" in trade
        assert "mfe_giveback_fraction" in trade
