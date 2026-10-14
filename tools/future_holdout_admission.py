"""Temporal gate for NEW data received only AFTER a candidate freeze.

This is source-file coverage verification, NOT independent cryptographic
provenance, not actual exchange execution and not a profitability decision.
No network, broker, trading permissions, or production state mutation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

SIX = ("BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_SOURCE = {
    "BYBIT_V5_PUBLIC_KLINE": "https://api.bybit.com/v5/market/kline",
    "BYBIT_CONNECTOR_PUBLIC_KLINE": "plugin://Bybit/get_kline",
    "TEST_FIXTURE_ONLY": None,
}


class FutureHoldoutAdmissionError(ValueError):
    pass


def _utc(text: str, label: str) -> datetime:
    if not isinstance(text, str):
        raise FutureHoldoutAdmissionError(f"{label} must be an ISO 8601 UTC string")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FutureHoldoutAdmissionError(f"invalid {label}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise FutureHoldoutAdmissionError(f"{label} must have explicit UTC timezone")
    return parsed.astimezone(UTC)


def _safe_manifest(path: Path) -> dict:
    if not path.is_file():
        raise FutureHoldoutAdmissionError(f"required manifest missing: {path.name}")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise FutureHoldoutAdmissionError("invalid manifest JSON") from exc
    if not isinstance(decoded, dict):
        raise FutureHoldoutAdmissionError("manifest must be a JSON object")
    return decoded


def _freeze(path: Path) -> tuple[dict, datetime, datetime]:
    data = _safe_manifest(path)
    if data.get("schema_version") != "astra-research-candidate-freeze-v1":
        raise FutureHoldoutAdmissionError("candidate freeze schema is unsupported")
    if data.get("research_only") is not True:
        raise FutureHoldoutAdmissionError("freeze manifest must be research-only")
    if data.get("strategy_promotion_allowed") is not False:
        raise FutureHoldoutAdmissionError("freeze cannot authorize trading")
    if not isinstance(data.get("candidate_id"), str) or not data["candidate_id"].strip():
        raise FutureHoldoutAdmissionError("candidate identity is absent")
    if not isinstance(data.get("candidate_git_sha"), str) or not _SHA40.fullmatch(\n        data["candidate_git_sha"]\n    ):
        raise FutureHoldoutAdmissionError("candidate Git SHA is invalid")
    for name in ("strategy_config_sha256", "selection_rules_sha256"):
        if not isinstance(data.get(name), str) or not _SHA256.fullmatch(data[name]):
            raise FutureHoldoutAdmissionError(f"{name} is invalid")
    if data.get("symbols") != list(SIX):
        raise FutureHoldoutAdmissionError("universe must be frozen and canonical")
    frozen = _utc(data.get("frozen_at_utc"), "frozen_at_utc")
    already_seen = _utc(
        data.get("previously_inspected_history_end_utc"),
        "previously_inspected_history_end_utc",
    )
    if already_seen > frozen:
        raise FutureHoldoutAdmissionError("history extends beyond declared freeze")
    if data.get("minimum_future_daily_bars") != 90:
        raise FutureHoldoutAdmissionError("minimum 90 future daily bars is mandatory")
    return data, frozen, already_seen


def validate_future_holdout(
    *, freeze_path: Path, source_directory: Path,
    assessment_as_of: datetime | None = None,
) -> dict:
    """Fail closed for stale, unclosed, modified, gapped or mixed-coin data.

    Even a passing result is only a TEMPORAL_COVERAGE_CHECK. Git author
    timestamps and provider authenticity are not verified by this module.
    """
    freeze, frozen, previously_seen = _freeze(freeze_path)
    assessed = datetime.now(UTC) if assessment_as_of is None else assessment_as_of
    if assessed.tzinfo is None or assessed.utcoffset() != timedelta(0):
        raise FutureHoldoutAdmissionError("assessment clock must be explicit UTC")
    assessed = assessed.astimezone(UTC)
    if assessed <= frozen:
        raise FutureHoldoutAdmissionError("candidate is not frozen before assessment")
    manifest = _safe_manifest(source_directory / "provenance.json")
    origin = manifest.get("data_origin")
    if origin not in _ALLOWED_SOURCE:
        raise FutureHoldoutAdmissionError("data origin is unsupported")
    fixture_only = origin == "TEST_FIXTURE_ONLY"
    if manifest.get("source_endpoint") != _ALLOWED_SOURCE[origin]:
        raise FutureHoldoutAdmissionError("source endpoint claim mismatches origin")
    if not fixture_only and not manifest.get("collector"):
        raise FutureHoldoutAdmissionError("public source collector claim is missing")
    if manifest.get("interval_minutes") != 1440:
        raise FutureHoldoutAdmissionError("only genuine daily source bars accepted")
    collected = _utc(manifest.get("collected_at_utc"), "collected_at_utc")
    if not (frozen < collected <= assessed):
        raise FutureHoldoutAdmissionError("collection not after freeze and before assessment")
    if manifest.get("symbols") != list(SIX):
        raise FutureHoldoutAdmissionError("data universe deviates from frozen universe")
    hashes = manifest.get("sha256")
    if not isinstance(hashes, dict) or set(hashes) != {
        f"{s}_D.csv" for s in SIX
    }:
        raise FutureHoldoutAdmissionError("all six exact source SHA digests required")
    timeline = None
    digest_report = {}
    total_bars = 0
    for symbol in SIX:
        name = f"{symbol}_D.csv"
        file = source_directory / name
        if not file.is_file() or file.is_symlink():
            raise FutureHoldoutAdmissionError("genuine local raw source file absent")
        claimed = hashes[name]
        if not isinstance(claimed, str) or not _SHA256.fullmatch(claimed):
            raise FutureHoldoutAdmissionError("invalid source digest format")
        actual = hashlib.sha256(file.read_bytes()).hexdigest()
        if actual != claimed:
            raise FutureHoldoutAdmissionError(f"mutated or unpinned source: {name}")
        with file.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != [
                "timestamp", "symbol", "open", "high", "low", "close", "volume"
            ]:
                raise FutureHoldoutAdmissionError("daily source CSV schema mismatch")
            stamps = []
            for row in reader:
                if row["symbol"] != symbol:
                    raise FutureHoldoutAdmissionError("cross-symbol contamination")
                stamp = _utc(row["timestamp"], "daily candle timestamp")
                if (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) != (
                    0, 0, 0, 0
                ):
                    raise FutureHoldoutAdmissionError("daily candle must start at UTC midnight")
                if stamp <= frozen or stamp <= previously_seen:
                    raise FutureHoldoutAdmissionError("historical/inspected bar reused as future")
                if stamp + timedelta(days=1) > assessed:
                    raise FutureHoldoutAdmissionError("unclosed/future daily candle included")
                if stamps and stamp != stamps[-1] + timedelta(days=1):
                    raise FutureHoldoutAdmissionError("missing, duplicate or reordered UTC day")
                try:
                    o, h, low, c, volume = (
                        Decimal(row[key])
                        for key in ("open", "high", "low", "close", "volume")
                    )
                except (InvalidOperation, TypeError, KeyError) as exc:
                    raise FutureHoldoutAdmissionError("malformed OHLCV numeric field") from exc
                if (
                    not all(v.is_finite() for v in (o, h, low, c, volume))
                    or min(o, h, low, c) <= 0
                    or volume < 0
                    or h < max(o, low, c)
                    or low > min(o, h, c)
                ):
                    raise FutureHoldoutAdmissionError("invalid raw OHLCV prices or volume")
                stamps.append(stamp)
        if len(stamps) < 90:
            raise FutureHoldoutAdmissionError("insufficient 90 completed future daily bars")
        if timeline is not None and tuple(stamps) != timeline:
            raise FutureHoldoutAdmissionError("daily dates inconsistent across symbols")
        timeline = tuple(stamps)
        total_bars += len(stamps)
        digest_report[name] = actual
    if timeline is None or collected < timeline[-1] + timedelta(days=1):
        raise FutureHoldoutAdmissionError("source collected before final bar close")
    return {
        "schema_version": "astra-post-freeze-daily-coverage-v1",
        "assessment_status": "TEMPORAL_SOURCE_COVERAGE_ONLY",
        "candidate_id": freeze["candidate_id"],
        "candidate_git_sha": freeze["candidate_git_sha"],
        "candidate_config_sha256": freeze["strategy_config_sha256"],
        "selection_rules_sha256": freeze["selection_rules_sha256"],
        "frozen_at_utc": frozen.isoformat(),
        "previously_inspected_history_end_utc": previously_seen.isoformat(),
        "first_candidate_future_day_utc": timeline[0].isoformat(),
        "last_completed_future_day_utc": timeline[-1].isoformat(),
        "distinct_days": len(timeline),
        "source_bars_total": total_bars,
        "source_sha256": dict(sorted(digest_report.items())),
        "test_fixture_only": fixture_only,
        "source_claim_is_self_declared": True,
        "provider_authenticity_independently_verified": False,
        "freeze_time_independently_witnessed": False,
        "untouched_holdout_independently_verified": False,
        "strategy_replay_completed": False,
        "broker_trading_allowed": False,
        "strategy_promotion_allowed": False,
        "profitability_proven": False,
        "research_data_usable_after_source_verification": not fixture_only,
        "reason": (
            "Timestamp and checksum coverage only. Independent provider "
            "provenance, freeze-time witness and full costed replay are required."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-freeze", type=Path, required=True)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate_future_holdout(
        freeze_path=args.candidate_freeze,
        source_directory=args.source_directory,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print("TEMPORAL_SOURCE_COVERAGE_ONLY", json.dumps({
        "days": report["distinct_days"],
        "source_sha256": report["source_sha256"],
        "profitability_proven": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
