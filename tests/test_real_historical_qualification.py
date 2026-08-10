from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.marketdata.historical import HistoricalDataPolicy
from app.marketdata.manifest import ManifestedCsvHistoricalBarSource
from tools.qualify_real_historical import qualify

DATA_DIR = Path("data/historical/aapl_plotly_multiregime")
MANIFEST = DATA_DIR / "manifest.json"
POLICY = DATA_DIR / "qualification.json"
EXPECTED_DATASET_SHA256 = "4242262f4d5d79352a43ec5cdc81f3a9d52953fd5ca2f230b3fabf890d33a256"
UPSTREAM_BLOB = "7b1bab3953bb5cdf47e84de1048ca04b0c991987"


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


def test_real_sample_qualification_uses_predeclared_acceptance_policy() -> None:
    evidence = qualify(MANIFEST, POLICY)
    assert evidence["qualified"] is True
    assert evidence["dataset_sha256"] == EXPECTED_DATASET_SHA256
    assert evidence["source_classification"] == "THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE"
    assert evidence["strategy_id"] == "paper-momentum-v1"
    regimes = evidence["regimes"]
    assert isinstance(regimes, list) and len(regimes) == 3
    assert all(item["bars"] == 20 for item in regimes)
    assert all(item["windows"] == 2 for item in regimes)
    assert all(item["qualified"] is True for item in regimes)


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
