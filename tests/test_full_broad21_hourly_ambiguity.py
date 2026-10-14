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

BROAD = Path("research/snapshots/bybit_ambiguous_broad_hourly_20261013")


def test_full_genuine_hourly_census_reconciles_all_24_cases() -> None:
    report = reconstruct_broad21(
        bars_dir=BARS,
        previously_pinned_hourly_dir=PRIOR,
        six_major_additional_dir=SIX,
        broad_additional_dir=BROAD,
    )
    assert report["complete_case_coverage"] is True
    assert report["summary"]["preselected_cases"] == 24
    assert report["summary"]["hourly_source_verified_cases"] == 24
    assert report["summary"]["missing_hourly_source_cases"] == 0
    assert report["summary"]["hourly_candles"] == 24 * 24
    assert sum(report["summary"]["first_touch_counts"].values()) == 24
    assert report["missing_cases_not_evidenced"] == []
    assert len(report["cases"]) == len(report["source_sha256"]) == 24
    assert all(len(x["source_sha256"]) == 64 for x in report["cases"])
    assert all(x["actual_exchange_fill_verified"] is False for x in report["cases"])
    assert report["counterfactual_portfolio_replayed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["live_trading_allowed"] is False
    assert report["untouched_holdout"] is False


def test_full_hourly_evidence_hash_and_threshold_counts_are_reproducible() -> None:
    settings = dict(
        bars_dir=BARS,
        previously_pinned_hourly_dir=PRIOR,
        six_major_additional_dir=SIX,
        broad_additional_dir=BROAD,
    )
    before = reconstruct_broad21(**settings)
    after = reconstruct_broad21(**settings)
    assert before == after
    assert len(before["evidence_sha256"]) == 64
    assert before["evidence_sha256"] == after["evidence_sha256"]
