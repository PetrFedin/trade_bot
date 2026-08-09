from __future__ import annotations

import copy
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.historical import CsvHistoricalBarSource, HistoricalDataPolicy
from app.strategy.backtest import BacktestConfig
from app.strategy.momentum import LongOnlyMomentumStrategy
from app.strategy.qualification import WalkForwardPolicy, WalkForwardQualifier
from app.strategy.qualification_manifest import (
    DatasetProvenance,
    QualificationManifestError,
    build_qualification_manifest,
    file_sha256,
    verify_qualification_manifest,
)
from app.strategy.regimes import HistoricalRegime, MultiRegimeQualifier

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "strategy_qualification"
DATASET = FIXTURE_DIR / "synthetic_regimes.csv"
SPEC = FIXTURE_DIR / "synthetic_spec.json"


def _manifest() -> dict[str, object]:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    dataset = CsvHistoricalBarSource(
        DATASET,
        policy=HistoricalDataPolicy(minimum_bars=75),
        expected_symbol="AAPL",
    ).load()
    strategy = LongOnlyMomentumStrategy(
        strategy_id="paper-momentum-v1", target_quantity=Decimal("1")
    )
    walk_forward = WalkForwardQualifier(
        strategy=strategy,
        backtest_config=BacktestConfig(
            opening_cash=Decimal("10000"),
            fee_per_fill=Decimal("0.01"),
            slippage_bps=Decimal("2"),
            minimum_history_bars=3,
        ),
        policy=WalkForwardPolicy(
            training_bars=8,
            testing_bars=4,
            step_bars=4,
            minimum_windows=3,
            maximum_drawdown_fraction=Decimal("0.05"),
            minimum_mean_oos_return=Decimal("-0.02"),
            minimum_mean_excess_return=Decimal("-0.05"),
        ),
    )
    regimes = tuple(
        HistoricalRegime(
            name=item["name"],
            start=datetime.fromisoformat(item["start"].replace("Z", "+00:00")),
            end=datetime.fromisoformat(item["end"].replace("Z", "+00:00")),
        )
        for item in spec["regimes"]
    )
    return build_qualification_manifest(
        dataset=dataset,
        provenance=DatasetProvenance(
            classification="synthetic_fixture",
            provider="fixture",
            source_reference="tests/fixtures/strategy_qualification/synthetic_regimes.csv",
        ),
        strategy=strategy,
        qualifier=MultiRegimeQualifier(qualifier=walk_forward, minimum_regimes=3),
        regimes=regimes,
        spec_sha256=file_sha256(SPEC),
    )


def test_manifest_is_deterministic_and_binds_all_qualification_inputs() -> None:
    first = _manifest()
    second = _manifest()
    assert first == second
    verify_qualification_manifest(first)
    assert first["schema_version"] == "strategy-qualification-manifest-v1"
    assert first["dataset"]["provenance"]["classification"] == "synthetic_fixture"
    assert first["strategy"]["implementation_sha256"]
    assert first["qualification_contract"]["spec_sha256"] == file_sha256(SPEC)
    assert len(first["result"]["regimes"]) == 3


def test_manifest_rejects_result_or_input_tampering() -> None:
    manifest = _manifest()
    result_tampered = copy.deepcopy(manifest)
    result_tampered["result"]["qualified"] = not result_tampered["result"]["qualified"]
    with pytest.raises(QualificationManifestError, match="result_sha256 mismatch"):
        verify_qualification_manifest(result_tampered)

    input_tampered = copy.deepcopy(manifest)
    input_tampered["strategy"]["parameters"]["target_quantity"] = "999"
    with pytest.raises(QualificationManifestError, match="qualification_id mismatch"):
        verify_qualification_manifest(input_tampered)


def test_external_historical_label_cannot_use_fixture_provider() -> None:
    provenance = DatasetProvenance(
        classification="external_historical",
        provider="fixture",
        source_reference="not-real.csv",
    )
    with pytest.raises(QualificationManifestError, match="cannot use a fixture"):
        provenance.validate()
