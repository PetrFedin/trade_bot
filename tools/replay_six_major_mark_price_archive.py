"""Fail-closed atomic offline import of the full six-major Mark Price source set.

All six archived Bybit response collections and their original funding sources
must independently verify BEFORE any published output exists. This module does
not acquire market data, certify exchange authorship, or replay trading PnL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from tools.atomic_directory_publish import AtomicPublicationError, publish_new_directory
from tools.mark_price_hourly_sensitivity import _utc
from tools.replay_mark_price_hourly_archive import (
    ArchiveReplayError,
    _pinned_file,
    replay_archived_symbol,
)
from tools.summarize_mark_price_hourly_sensitivity import SIX, summarize
from tools.verify_mark_price_hourly_archive import verify_symbol_archive

SCHEMA = "astra-six-major-hourly-mark-source-set-v1"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class SourceSetError(ValueError):
    """Reject an incomplete, unapproved, or mismatched six-symbol source set."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _separate_paths(target: Path, archived_root: Path, funding_root: Path) -> None:
    destination = target.resolve()
    for source in (archived_root, funding_root):
        resolved = source.resolve()
        if (
            destination == resolved
            or destination.is_relative_to(resolved)
            or resolved.is_relative_to(destination)
        ):
            raise SourceSetError("output directory must not overlap source archives")
    if any(p.is_symlink() for p in (target, archived_root, funding_root)):
        raise SourceSetError("symlinked root or output is forbidden")


def _load_source_set(path: Path, expected_sha256: str) -> tuple[dict, datetime, datetime]:
    try:
        raw = _pinned_file(path, expected_sha256)
        data = json.loads(raw)
    except (ArchiveReplayError, UnicodeError, ValueError) as exc:
        raise SourceSetError(f"detached source-set approval invalid: {exc}") from exc
    if not isinstance(data, dict):
        raise SourceSetError("source-set document must be a JSON object")
    pins = data.get("mark_manifest_sha256")
    if (
        data.get("schema_version") != SCHEMA
        or data.get("symbols") != list(SIX)
        or not isinstance(pins, dict)
        or set(pins) != set(SIX)
        or not all(
            isinstance(pin, str) and SHA256_PATTERN.fullmatch(pin)
            for pin in pins.values()
        )
        or data.get("research_only") is not True
        or data.get("origin_claim") != "UNSIGNED_PUBLIC_PROVIDER_RESPONSE"
    ):
        raise SourceSetError("incomplete or unsupported six-major source-set contract")
    for field in (
        "provider_origin_verified", "account_funding_ledger_verified",
        "exact_settlement_mark_verified", "strategy_replay_completed",
        "strategy_promotion_allowed", "live_trading_allowed",
        "profitability_proven",
    ):
        if data.get(field) is not False:
            raise SourceSetError(f"unsafe source-set claim: {field}")
    try:
        start = _utc(data["start"], "start")
        end = _utc(data["end"], "end")
    except (KeyError, ValueError) as exc:
        raise SourceSetError("invalid source-set UTC dates") from exc
    if end < start or any(
        (value.minute, value.second, value.microsecond) != (0, 0, 0)
        for value in (start, end)
    ):
        raise SourceSetError("source-set chronology must be contiguous UTC hours")
    return data, start, end


def replay_six_major_source_set(
    *,
    source_set_path: Path,
    expected_source_set_sha256: str,
    archived_root: Path,
    funding_root: Path,
    output_dir: Path,
) -> dict:
    """Validate every symbol, reproduce every page, publish ALL SIX atomically.

    Detached SHA pins must be approved through an independently controlled
    channel. Source-byte reproducibility is not proof of provider authenticity.
    """
    source_set_path = Path(source_set_path)
    archived_root = Path(archived_root)
    funding_root = Path(funding_root)
    output_dir = Path(output_dir)
    _separate_paths(output_dir, archived_root, funding_root)
    if output_dir.exists() or output_dir.is_symlink():
        raise SourceSetError("output directory already exists; no overwrite allowed")
    source_set, start, end = _load_source_set(
        source_set_path, expected_source_set_sha256
    )
    expected_hours = int((end - start) / timedelta(hours=1)) + 1
    # Preflight ALL source manifests, funding rates and raw response pages before
    # allocating a published destination or accepting even one symbol.
    for symbol in SIX:
        file = archived_root / symbol / f"{symbol}_mark_60m.manifest.json"
        try:
            meta_bytes = _pinned_file(
                file, source_set["mark_manifest_sha256"][symbol]
            )
            meta = json.loads(meta_bytes)
            if (
                not isinstance(meta, dict)
                or meta.get("start") != start.isoformat()
                or meta.get("end") != end.isoformat()
            ):
                raise SourceSetError(f"{symbol}: mixed source windows")
            checks = verify_symbol_archive(
                root=archived_root,
                funding_root=funding_root,
                symbol=symbol,
                mark_manifest=meta,
            )
            if checks["hourly_rows"] != expected_hours:
                raise SourceSetError(f"{symbol}: source hour count mismatch")
        except (ArchiveReplayError, ValueError, KeyError) as exc:
            raise SourceSetError(f"{symbol}: source-set preflight failed: {exc}") from exc

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".astra-six-major-staging-", dir=output_dir.parent
    ) as tmp:
        stage = Path(tmp) / "qualified"
        stage.mkdir()
        receipts = {}
        for symbol in SIX:
            try:
                receipt = replay_archived_symbol(
                    archived_root=archived_root,
                    funding_root=funding_root,
                    output_root=stage,
                    symbol=symbol,
                    start=start,
                    end=end,
                    expected_manifest_sha256=source_set[
                        "mark_manifest_sha256"
                    ][symbol],
                )
            except (ArchiveReplayError, ValueError) as exc:
                raise SourceSetError(
                    f"{symbol}: replay failed; no source set published: {exc}"
                ) from exc
            receipts[symbol] = receipt

        # Full six-source summary verifies hashes, settlement join, page
        # provenance and shared chronology; partial sets cannot produce it.
        summary = summarize(stage, funding_root=funding_root)
        if (
            summary["symbols"] != list(SIX)
            or summary["start"] != start.isoformat()
            or summary["end"] != end.isoformat()
            or summary["hourly_rows_total"] != expected_hours * len(SIX)
            or summary["strategy_promotion_allowed"] is not False
            or summary["profitability_proven"] is not False
        ):
            raise SourceSetError("six-major summary qualification mismatch")
        summary_bytes = (
            json.dumps(summary, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        (stage / "summary.json").write_bytes(summary_bytes)
        result = {
            "schema_version": "astra-six-major-offline-source-set-receipt-v1",
            "source_set_sha256": expected_source_set_sha256,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "symbols": list(SIX),
            "symbol_receipts": receipts,
            "summary_sha256": _sha(summary_bytes),
            "hourly_rows_total": summary["hourly_rows_total"],
            "funding_settlements_total": summary["funding_settlements_total"],
            "source_hash_consistency_verified": True,
            "provider_origin_verified": False,
            "account_funding_ledger_verified": False,
            "exact_settlement_mark_verified": False,
            "strategy_replay_completed": False,
            "strategy_promotion_allowed": False,
            "live_trading_allowed": False,
            "profitability_proven": False,
        }
        (stage / "source-set-receipt.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # All-or-nothing directory rename on the same filesystem.
        # Recheck to refuse a target created during the expensive preflight.
        if output_dir.exists() or output_dir.is_symlink():
            raise SourceSetError("output directory appeared during qualification")
        try:
            publish_new_directory(stage, output_dir)
        except AtomicPublicationError as exc:
            raise SourceSetError(f"atomic source-set publication refused: {exc}") from exc
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-set", type=Path, required=True)
    parser.add_argument("--source-set-sha256", required=True)
    parser.add_argument("--archived-root", type=Path, required=True)
    parser.add_argument("--funding-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = replay_six_major_source_set(
        source_set_path=args.source_set,
        expected_source_set_sha256=args.source_set_sha256,
        archived_root=args.archived_root,
        funding_root=args.funding_root,
        output_dir=args.output_dir,
    )
    print("SIX_MAJOR_MARK_REPLAY_RESEARCH_ONLY", json.dumps({
        "symbols": report["symbols"],
        "source_set_sha256": report["source_set_sha256"],
        "summary_sha256": report["summary_sha256"],
        "hourly_rows": report["hourly_rows_total"],
        "profitability_proven": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
