"""Negative proof: old, synthetic, incomplete data cannot become OOS edge."""
import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.future_holdout_admission import (
    SIX,
    FutureHoldoutAdmissionError,
    validate_future_holdout,
)

FREEZE = datetime(2024, 1, 1, 12, tzinfo=UTC)
FIRST = datetime(2024, 1, 3, tzinfo=UTC)
ASSESS = datetime(2024, 4, 21, tzinfo=UTC)


def _fixture(
    root: Path, *,
    days: int = 90,
    source: str = "TEST_FIXTURE_ONLY",
    gap: bool = False,
):
    root.mkdir(parents=True, exist_ok=True)
    freeze = root / "candidate-freeze.json"
    source_directory = root / "bars"
    source_directory.mkdir()
    candidate = {
        "schema_version": "astra-research-candidate-freeze-v1",
        "candidate_id": "TEST_NOT_REAL_CANDIDATE",
        "candidate_git_sha": "a" * 40,
        "strategy_config_sha256": "b" * 64,
        "selection_rules_sha256": "c" * 64,
        "frozen_at_utc": FREEZE.isoformat(),
        "previously_inspected_history_end_utc": (
            FREEZE - timedelta(days=3)
        ).isoformat(),
        "symbols": list(SIX),
        "minimum_future_daily_bars": 90,
        "research_only": True,
        "strategy_promotion_allowed": False,
    }
    freeze.write_text(json.dumps(candidate))
    claimed_sha = {}
    for symbol in SIX:
        path = source_directory / f"{symbol}_D.csv"
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([
                "timestamp", "symbol", "open", "high", "low", "close", "volume",
            ])
            for index in range(days):
                offset = index + (1 if gap and symbol == SIX[0] and index > 10 else 0)
                day = FIRST + timedelta(days=offset)
                writer.writerow([
                    day.isoformat(), symbol, "100", "102", "99", "101", "1000",
                ])
        claimed_sha[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    provenance = {
        "data_origin": source,
        "source_endpoint": (
            None if source == "TEST_FIXTURE_ONLY"
            else "https://api.bybit.com/v5/market/kline"
        ),
        "collector": "UNTRUSTED_TEST_DECLARATION",
        "interval_minutes": 1440,
        "collected_at_utc": datetime(2024, 4, 20, tzinfo=UTC).isoformat(),
        "symbols": list(SIX),
        "sha256": claimed_sha,
    }
    (source_directory / "provenance.json").write_text(json.dumps(provenance))
    return freeze, source_directory


def test_temporal_fixture_is_never_real_provider_or_profitable(tmp_path: Path) -> None:
    freeze, source = _fixture(tmp_path)
    report = validate_future_holdout(
        freeze_path=freeze,
        source_directory=source,
        assessment_as_of=ASSESS,
    )
    assert report["distinct_days"] == 90
    assert report["source_bars_total"] == 540
    assert report["test_fixture_only"] is True
    assert report["assessment_status"] == "TEMPORAL_SOURCE_COVERAGE_ONLY"
    assert report["provider_authenticity_independently_verified"] is False
    assert report["freeze_time_independently_witnessed"] is False
    assert report["untouched_holdout_independently_verified"] is False
    assert report["profitability_proven"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["broker_trading_allowed"] is False
    assert report["research_data_usable_after_source_verification"] is False


def test_self_declared_bybit_still_not_independently_verified(tmp_path: Path) -> None:
    freeze, source = _fixture(tmp_path, source="BYBIT_V5_PUBLIC_KLINE")
    report = validate_future_holdout(
        freeze_path=freeze, source_directory=source,
        assessment_as_of=ASSESS,
    )
    assert report["source_claim_is_self_declared"] is True
    assert report["test_fixture_only"] is False
    assert report["provider_authenticity_independently_verified"] is False
    assert report["untouched_holdout_independently_verified"] is False
    assert report["research_data_usable_after_source_verification"] is True
    assert report["strategy_replay_completed"] is False
    assert report["profitability_proven"] is False


def test_historical_data_stamped_before_candidate_freeze_rejected(tmp_path: Path) -> None:
    freeze, source = _fixture(tmp_path)
    manifest = json.loads(freeze.read_text())
    manifest["frozen_at_utc"] = "2024-02-01T00:00:00+00:00"
    freeze.write_text(json.dumps(manifest))
    with pytest.raises(FutureHoldoutAdmissionError, match="historical/inspected"):
        validate_future_holdout(
            freeze_path=freeze, source_directory=source,
            assessment_as_of=ASSESS,
        )


def test_tampered_csv_never_passes_digest_verification(tmp_path: Path) -> None:
    freeze, source = _fixture(tmp_path)
    file = source / f"{SIX[0]}_D.csv"
    file.write_text(file.read_text().replace("1000", "1100", 1))
    with pytest.raises(FutureHoldoutAdmissionError, match="mutated or unpinned"):
        validate_future_holdout(
            freeze_path=freeze, source_directory=source,
            assessment_as_of=ASSESS,
        )


def test_short_history_rejected_even_if_source_looks_consistent(tmp_path: Path) -> None:
    freeze, source = _fixture(tmp_path, days=89)
    with pytest.raises(FutureHoldoutAdmissionError, match="insufficient 90"):
        validate_future_holdout(
            freeze_path=freeze, source_directory=source,
            assessment_as_of=ASSESS,
        )


def test_gap_or_misaligned_coin_dates_rejected(tmp_path: Path) -> None:
    freeze, source = _fixture(tmp_path, gap=True)
    with pytest.raises(FutureHoldoutAdmissionError, match="missing, duplicate"):
        validate_future_holdout(
            freeze_path=freeze, source_directory=source,
            assessment_as_of=ASSESS,
        )


def test_unclosed_future_daily_candle_rejected(tmp_path: Path) -> None:
    freeze, source = _fixture(tmp_path)
    provenance_file = source / "provenance.json"
    metadata = json.loads(provenance_file.read_text())
    metadata["collected_at_utc"] = "2024-02-10T00:00:00+00:00"
    provenance_file.write_text(json.dumps(metadata))
    with pytest.raises(FutureHoldoutAdmissionError, match="unclosed/future"):
        validate_future_holdout(
            freeze_path=freeze, source_directory=source,
            assessment_as_of=datetime(2024, 2, 11, tzinfo=UTC),
        )


def test_untrusted_candidate_sha_and_unproven_source_block_claims(tmp_path: Path) -> None:
    freeze, source = _fixture(tmp_path)
    meta = json.loads(freeze.read_text())
    meta["candidate_git_sha"] = None
    freeze.write_text(json.dumps(meta))
    with pytest.raises(FutureHoldoutAdmissionError, match="Git SHA"):
        validate_future_holdout(
            freeze_path=freeze, source_directory=source,
            assessment_as_of=ASSESS,
        )
    meta["candidate_git_sha"] = "a" * 40
    meta["strategy_promotion_allowed"] = True
    freeze.write_text(json.dumps(meta))
    with pytest.raises(FutureHoldoutAdmissionError, match="authorize trading"):
        validate_future_holdout(
            freeze_path=freeze, source_directory=source,
            assessment_as_of=ASSESS,
        )


def test_no_applicable_market_period_until_future_collection(tmp_path: Path) -> None:
    freeze, source = _fixture(tmp_path)
    data = json.loads(freeze.read_text())
    data["frozen_at_utc"] = "2026-10-14T17:00:00+00:00"
    data["previously_inspected_history_end_utc"] = "2026-10-08T00:00:00+00:00"
    freeze.write_text(json.dumps(data))
    provenance_path = source / "provenance.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["collected_at_utc"] = "2027-01-20T00:00:00+00:00"
    provenance_path.write_text(json.dumps(provenance))
    with pytest.raises(FutureHoldoutAdmissionError, match="historical/inspected"):
        validate_future_holdout(
            freeze_path=freeze, source_directory=source,
            assessment_as_of=datetime(2027, 1, 31, tzinfo=UTC),
        )
