from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.marketdata.historical import HistoricalDataPolicy
from app.marketdata.manifest import ManifestedCsvHistoricalBarSource
from app.strategy.benchmarks import CAPITAL_MATCHED_BUY_HOLD_V1
from tools.qualify_real_historical import load_policy, qualify

DATA_DIR = Path("data/historical/aapl_plotly_multiregime")
MANIFEST = DATA_DIR / "manifest.json"
POLICY = DATA_DIR / "qualification.json"
EXPECTED_DATASET_SHA256 = "4242262f4d5d79352a43ec5cdc81f3a9d52953fd5ca2f230b3fabf890d33a256"
UPSTREAM_BLOB = "7b1bab3953bb5cdf47e84de1048ca04b0c991987"
EXPECTED_CAPITAL_MATCHED_MEANS = {
    "rising_2015_q4": "-0.000152531749925",
    "drawdown_2016_spring": "-0.000494520050075",
    "range_2016_q4": "0.00015406379995",
}


def test_manifested_snapshot_is_hash_locked_and_windowed() -> None:
    manifested = ManifestedCsvHistoricalBarSource(
        MANIFEST,
        policy=HistoricalDataPolicy(minimum_bars=60),
    ).load()
    assert manifested.dataset.symbol == "AAPL"
    assert len(manifested.dataset.bars) == 60
    assert manifested.dataset.canonical_sha256 == EXPECTED_DATASET_SHA256
    assert manifested.manifest.upstream_git_blob_sha == UPSTREAM_BLOB
    assert manifested.manifest.source_classification == "THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE"
    assert [window.name for window in manifested.manifest.windows] == [
        "rising_2015_q4",
        "drawdown_2016_spring",
        "range_2016_q4",
    ]


def test_real_sample_qualification_uses_capital_matched_benchmark_policy() -> None:
    evidence = qualify(MANIFEST, POLICY)
    assert evidence["qualified"] is True
    assert evidence["dataset_sha256"] == EXPECTED_DATASET_SHA256
    assert evidence["source_classification"] == "THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE"
    assert evidence["strategy_id"] == "paper-momentum-v1"
    assert evidence["benchmark_mode"] == CAPITAL_MATCHED_BUY_HOLD_V1
    acceptance_policy = evidence["acceptance_policy"]
    assert isinstance(acceptance_policy, dict)
    assert acceptance_policy["benchmark_mode"] == CAPITAL_MATCHED_BUY_HOLD_V1

    regimes = evidence["regimes"]
    assert isinstance(regimes, list) and len(regimes) == 3
    assert all(item["bars"] == 20 for item in regimes)
    assert all(item["windows"] == 2 for item in regimes)
    assert all(item["qualified"] is True for item in regimes)
    for regime in regimes:
        assert regime["benchmark_mode"] == CAPITAL_MATCHED_BUY_HOLD_V1
        assert regime["mean_cash_benchmark_return"] == "0"
        assert regime["mean_capital_matched_benchmark_return"] == (
            EXPECTED_CAPITAL_MATCHED_MEANS[regime["name"]]
        )
        assert len(regime["window_baselines"]) == 2


def test_policy_rejects_legacy_unmatched_benchmark_semantics(tmp_path: Path) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy.pop("benchmark_mode")
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(ValueError, match="benchmark mode mismatch"):
        load_policy(path)


def test_snapshot_tampering_is_rejected_before_qualification(tmp_path: Path) -> None:
    manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    copied_bars = tmp_path / "bars.csv"
    content = (DATA_DIR / "bars.csv").read_text(encoding="utf-8")
    copied_bars.write_text(content.replace("110.209999", "110.219999", 1), encoding="utf-8")
    copied_manifest = tmp_path / "manifest.json"
    copied_manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
    with pytest.raises(ValueError, match="HISTORICAL_SNAPSHOT_SHA256_MISMATCH"):
        ManifestedCsvHistoricalBarSource(copied_manifest).load()


def test_manifest_rejects_path_escape(tmp_path: Path) -> None:
    manifest_data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest_data["dataset_file"] = "../bars.csv"
    copied_manifest = tmp_path / "manifest.json"
    copied_manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
    with pytest.raises(ValueError, match="within the manifest directory"):
        ManifestedCsvHistoricalBarSource(copied_manifest).load()
