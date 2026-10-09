"""Fail-closed guard for ASTRA's preregistered final untouched holdout.

This module deliberately contains no strategy, backtester, PnL or benchmark-evaluation
imports. Before the preregistered evaluation boundary it may only validate the frozen
candidate identity and the completeness/continuity of raw daily data.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class HoldoutAssessment:
    status: str
    evaluation_allowed: bool
    strategy_promotion_allowed: bool
    candidate_match: bool
    time_ready: bool
    data_ready: bool
    complete_common_bars: int
    required_complete_bars: int
    pnl_evaluation_performed: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "evaluation_allowed": self.evaluation_allowed,
            "strategy_promotion_allowed": self.strategy_promotion_allowed,
            "candidate_match": self.candidate_match,
            "time_ready": self.time_ready,
            "data_ready": self.data_ready,
            "complete_common_bars": self.complete_common_bars,
            "required_complete_bars": self.required_complete_bars,
            "pnl_evaluation_performed": self.pnl_evaluation_performed,
        }


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("holdout timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def load_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_manifest(payload)
    return payload


def validate_manifest(payload: dict[str, object]) -> None:
    if payload.get("schema_version") != "astra-final-untouched-holdout-v1":
        raise ValueError("unsupported final holdout schema")
    if payload.get("research_only") is not True:
        raise ValueError("final holdout must remain research-only")
    if payload.get("strategy_promotion_allowed") is not False:
        raise ValueError("preregistered holdout cannot pre-authorize promotion")
    if payload.get("holdout_role") != "FROZEN_CONTROL_ONLY":
        raise ValueError("Strategy A holdout must remain a frozen control")
    candidate_sha = payload.get("candidate_source_sha")
    if not isinstance(candidate_sha, str) or len(candidate_sha) != 40:
        raise ValueError("candidate_source_sha must be a 40-character git SHA")

    symbols = payload.get("primary_universe")
    if not isinstance(symbols, list) or len(symbols) < 2:
        raise ValueError("primary_universe must contain at least two symbols")
    if symbols != sorted(set(symbols)):
        raise ValueError("primary_universe must be unique and canonically sorted")

    holdout = payload.get("holdout")
    if not isinstance(holdout, dict):
        raise ValueError("holdout block missing")
    start = _parse_utc(str(holdout["start"]))
    evaluation_not_before = _parse_utc(str(holdout["evaluation_not_before"]))
    minimum = int(holdout["minimum_complete_daily_bars"])
    if minimum < 180:
        raise ValueError("final holdout must contain at least 180 complete daily bars")
    expected_boundary = start + timedelta(days=minimum)
    if evaluation_not_before < expected_boundary:
        raise ValueError("evaluation boundary precedes completion of required bars")

    blockers = payload.get("preexisting_promotion_blockers")
    if not isinstance(blockers, list) or not blockers:
        raise ValueError("preexisting promotion blockers must remain explicit")
    semantics = payload.get("promotion_semantics")
    if not isinstance(semantics, dict):
        raise ValueError("promotion semantics missing")
    if semantics.get("successful_holdout_cannot_clear_preexisting_blockers") is not True:
        raise ValueError("holdout cannot erase preexisting negative evidence")


def _read_timestamps(path: Path) -> tuple[datetime, ...]:
    seen: set[datetime] = set()
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            timestamp = _parse_utc(row["timestamp"])
            if timestamp in seen:
                raise ValueError(f"duplicate holdout timestamp: {path.name}:{timestamp}")
            seen.add(timestamp)
    return tuple(sorted(seen))


def inspect_complete_common_bars(
    manifest: dict[str, object],
    *,
    bars_dir: Path,
    as_of: datetime,
) -> tuple[int, bool]:
    as_of = as_of.astimezone(UTC)
    holdout = manifest["holdout"]
    start = _parse_utc(str(holdout["start"]))
    minimum = int(holdout["minimum_complete_daily_bars"])
    required = tuple(start + timedelta(days=index) for index in range(minimum))

    per_symbol: list[set[datetime]] = []
    for symbol in manifest["primary_universe"]:
        path = bars_dir / f"{symbol}_D.csv"
        if not path.is_file():
            return 0, False
        timestamps = {
            timestamp
            for timestamp in _read_timestamps(path)
            if timestamp >= start and timestamp + timedelta(days=1) <= as_of
        }
        per_symbol.append(timestamps)

    common = set.intersection(*per_symbol)
    complete_prefix = 0
    for timestamp in required:
        if timestamp not in common:
            break
        complete_prefix += 1
    data_ready = complete_prefix >= minimum
    return complete_prefix, data_ready


def assess_holdout(
    manifest: dict[str, object],
    *,
    candidate_source_sha: str,
    bars_dir: Path | None,
    as_of: datetime,
) -> HoldoutAssessment:
    validate_manifest(manifest)
    holdout = manifest["holdout"]
    evaluation_not_before = _parse_utc(str(holdout["evaluation_not_before"]))
    required = int(holdout["minimum_complete_daily_bars"])
    candidate_match = candidate_source_sha == manifest["candidate_source_sha"]
    time_ready = as_of.astimezone(UTC) >= evaluation_not_before

    complete_common_bars = 0
    data_ready = False
    if bars_dir is not None:
        complete_common_bars, data_ready = inspect_complete_common_bars(
            manifest,
            bars_dir=bars_dir,
            as_of=as_of,
        )

    evaluation_allowed = candidate_match and time_ready and data_ready
    if not candidate_match:
        status = "CANDIDATE_MISMATCH_BLOCKED"
    elif not time_ready:
        status = "SEALED_COLLECTION_ONLY"
    elif not data_ready:
        status = "EVALUATION_BLOCKED_INSUFFICIENT_DATA"
    else:
        status = "EVALUATION_ELIGIBLE_CONTROL_ONLY"

    return HoldoutAssessment(
        status=status,
        evaluation_allowed=evaluation_allowed,
        strategy_promotion_allowed=False,
        candidate_match=candidate_match,
        time_ready=time_ready,
        data_ready=data_ready,
        complete_common_bars=complete_common_bars,
        required_complete_bars=required,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--candidate-source-sha")
    parser.add_argument("--bars-dir", type=Path)
    parser.add_argument("--contract-only", action="store_true")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    if args.contract_only:
        output = {
            "status": "PREREGISTERED_SEALED_CONTROL",
            "candidate_source_sha": manifest["candidate_source_sha"],
            "holdout": manifest["holdout"],
            "strategy_promotion_allowed": False,
            "pnl_evaluation_performed": False,
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0

    if args.candidate_source_sha is None:
        parser.error("--candidate-source-sha is required outside --contract-only")
    assessment = assess_holdout(
        manifest,
        candidate_source_sha=args.candidate_source_sha,
        bars_dir=args.bars_dir,
        as_of=datetime.now(UTC),
    )
    print(json.dumps(assessment.payload(), indent=2, sort_keys=True))
    return 0 if assessment.evaluation_allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
