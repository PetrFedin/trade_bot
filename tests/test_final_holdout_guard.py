from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.final_holdout_guard import (
    assess_holdout,
    load_manifest,
    validate_manifest,
)


MANIFEST = Path("research/final_untouched_holdout_strategy_a_v1.json")


def _write_bars(
    root: Path,
    symbols: list[str],
    *,
    count: int,
    missing_index: int | None = None,
    duplicate_index: int | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 10, 11, tzinfo=UTC)
    for symbol in symbols:
        path = root / f"{symbol}_D.csv"
        rows = []
        for index in range(count):
            if missing_index is not None and index == missing_index:
                continue
            timestamp = start + timedelta(days=index)
            rows.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "symbol": symbol,
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100",
                    "volume": "1000",
                }
            )
            if duplicate_index is not None and index == duplicate_index:
                rows.append(dict(rows[-1]))
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "timestamp",
                    "symbol",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)


def _manifest() -> dict[str, object]:
    return load_manifest(MANIFEST)


def test_preregistered_manifest_is_frozen_and_non_promotable() -> None:
    manifest = _manifest()

    validate_manifest(manifest)

    assert manifest["research_only"] is True
    assert manifest["strategy_promotion_allowed"] is False
    assert manifest["holdout_role"] == "FROZEN_CONTROL_ONLY"
    assert manifest["candidate_source_sha"] == (
        "e053bba6e69e900844806e5c61f53e56fe296c90"
    )
    assert manifest["holdout"]["start"] == "2026-10-11T00:00:00Z"
    assert manifest["holdout"]["minimum_complete_daily_bars"] == 180
    assert manifest["holdout"]["evaluation_not_before"] == "2027-04-09T00:00:00Z"
    assert manifest["preexisting_promotion_blockers"]


def test_early_time_remains_sealed_even_with_available_raw_bars(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    bars = tmp_path / "bars"
    _write_bars(bars, manifest["primary_universe"], count=180)

    assessment = assess_holdout(
        manifest,
        candidate_source_sha=manifest["candidate_source_sha"],
        bars_dir=bars,
        as_of=datetime(2027, 4, 8, 23, 59, tzinfo=UTC),
    )

    assert assessment.status == "SEALED_COLLECTION_ONLY"
    assert assessment.evaluation_allowed is False
    assert assessment.strategy_promotion_allowed is False
    assert assessment.time_ready is False
    assert assessment.pnl_evaluation_performed is False


def test_boundary_with_only_179_complete_bars_is_blocked(tmp_path: Path) -> None:
    manifest = _manifest()
    bars = tmp_path / "bars"
    _write_bars(bars, manifest["primary_universe"], count=179)

    assessment = assess_holdout(
        manifest,
        candidate_source_sha=manifest["candidate_source_sha"],
        bars_dir=bars,
        as_of=datetime(2027, 4, 9, tzinfo=UTC),
    )

    assert assessment.status == "EVALUATION_BLOCKED_INSUFFICIENT_DATA"
    assert assessment.time_ready is True
    assert assessment.data_ready is False
    assert assessment.complete_common_bars == 179
    assert assessment.evaluation_allowed is False


def test_exact_boundary_with_180_complete_common_bars_is_control_only_eligible(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    bars = tmp_path / "bars"
    _write_bars(bars, manifest["primary_universe"], count=180)

    assessment = assess_holdout(
        manifest,
        candidate_source_sha=manifest["candidate_source_sha"],
        bars_dir=bars,
        as_of=datetime(2027, 4, 9, tzinfo=UTC),
    )

    assert assessment.status == "EVALUATION_ELIGIBLE_CONTROL_ONLY"
    assert assessment.time_ready is True
    assert assessment.data_ready is True
    assert assessment.complete_common_bars == 180
    assert assessment.evaluation_allowed is True
    assert assessment.strategy_promotion_allowed is False
    assert assessment.pnl_evaluation_performed is False


def test_candidate_mismatch_blocks_even_after_time_and_data_are_ready(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    bars = tmp_path / "bars"
    _write_bars(bars, manifest["primary_universe"], count=180)

    assessment = assess_holdout(
        manifest,
        candidate_source_sha="0" * 40,
        bars_dir=bars,
        as_of=datetime(2027, 4, 9, tzinfo=UTC),
    )

    assert assessment.status == "CANDIDATE_MISMATCH_BLOCKED"
    assert assessment.data_ready is True
    assert assessment.time_ready is True
    assert assessment.evaluation_allowed is False


def test_gap_breaks_required_contiguous_prefix(tmp_path: Path) -> None:
    manifest = _manifest()
    bars = tmp_path / "bars"
    _write_bars(
        bars,
        manifest["primary_universe"],
        count=180,
        missing_index=57,
    )

    assessment = assess_holdout(
        manifest,
        candidate_source_sha=manifest["candidate_source_sha"],
        bars_dir=bars,
        as_of=datetime(2027, 4, 9, tzinfo=UTC),
    )

    assert assessment.status == "EVALUATION_BLOCKED_INSUFFICIENT_DATA"
    assert assessment.complete_common_bars == 57
    assert assessment.data_ready is False


def test_duplicate_timestamp_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest()
    bars = tmp_path / "bars"
    _write_bars(
        bars,
        manifest["primary_universe"],
        count=180,
        duplicate_index=12,
    )

    with pytest.raises(ValueError, match="duplicate holdout timestamp"):
        assess_holdout(
            manifest,
            candidate_source_sha=manifest["candidate_source_sha"],
            bars_dir=bars,
            as_of=datetime(2027, 4, 9, tzinfo=UTC),
        )


def test_missing_symbol_dataset_is_blocked(tmp_path: Path) -> None:
    manifest = _manifest()
    bars = tmp_path / "bars"
    _write_bars(bars, manifest["primary_universe"][:-1], count=180)

    assessment = assess_holdout(
        manifest,
        candidate_source_sha=manifest["candidate_source_sha"],
        bars_dir=bars,
        as_of=datetime(2027, 4, 9, tzinfo=UTC),
    )

    assert assessment.status == "EVALUATION_BLOCKED_INSUFFICIENT_DATA"
    assert assessment.complete_common_bars == 0
    assert assessment.evaluation_allowed is False
