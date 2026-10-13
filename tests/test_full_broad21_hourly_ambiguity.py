"""Fail-closed broad-21 historical intraday source coverage tests."""
from pathlib import Path

from tools.full_broad21_hourly_ambiguity import reconstruct_broad21

BARS = Path("research/snapshots/bybit_3y_2023-10-09_2026-10-08")
PRIOR = Path("research/snapshots/bybit_intraday_trade_cases_60m")
SIX = Path("research/snapshots/bybit_ambiguous_six_hourly_20261013")


def test_missing_historical_provider_bars_cannot_be_certified(tmp_path: Path) -> None:
    report = reconstruct_broad21(
        bars_dir=BARS,
        previously_pinned_hourly_dir=PRIOR,
        six_major_additional_dir=SIX,
        broad_additional_dir=tmp_path,
    )
    assert report["summary"]["preselected_cases"] == 24
    assert report["summary"]["hourly_source_verified_cases"] == 4
    assert report["summary"]["missing_hourly_source_cases"] == 20
    assert report["summary"]["hourly_candles"] == 96
    assert report["complete_case_coverage"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["live_trading_allowed"] is False
    assert report["counterfactual_portfolio_replayed"] is False
    assert report["actual_exchange_fills_verified"] is False
    assert len(report["missing_cases_not_evidenced"]) == 20


def test_repeated_partial_evidence_is_deterministic(tmp_path: Path) -> None:
    kwargs = dict(
        bars_dir=BARS,
        previously_pinned_hourly_dir=PRIOR,
        six_major_additional_dir=SIX,
        broad_additional_dir=tmp_path,
    )
    a = reconstruct_broad21(**kwargs)
    b = reconstruct_broad21(**kwargs)
    assert a == b
    assert a["evidence_sha256"] == b["evidence_sha256"]
