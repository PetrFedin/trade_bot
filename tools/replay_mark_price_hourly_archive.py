"""Rebuild a previously captured Bybit hourly Mark Price archive without network.

Admission requires a *detached*, explicitly supplied SHA-256 of the source
manifest, complete retained canonical response pages, and the separate original
funding archive. It reconstructs and independently verifies deterministic CSV
and manifests. Content integrity is NOT exchange authenticity or trade proof.

Research-only. No broker, paper, live, selection, or strategy permissions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from tools.atomic_directory_publish import publish_new_directory
from tools.mark_price_hourly_sensitivity import (
    URL,
    _canonical,
    _utc,
    collect_symbol,
)
from tools.verify_mark_price_hourly_archive import verify_symbol_archive


class ArchiveReplayError(ValueError):
    """Refuse incomplete, unpinned, mutated, or non-reproducible evidence."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pinned_file(path: Path, expected_sha256: str) -> bytes:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ArchiveReplayError("invalid detached source SHA-256 pin")
    if not path.is_file() or path.is_symlink():
        raise ArchiveReplayError("missing or symlinked pinned source manifest")
    raw = path.read_bytes()
    if _sha(raw) != expected_sha256:
        raise ArchiveReplayError("detached source manifest SHA-256 mismatch")
    return raw


class _OfflineResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


class _PinnedPageReader:
    """Only serve exact pre-recorded pages; deliberately no HTTP fallback."""

    def __init__(self, source_dir: Path, manifest: dict):
        self.source_dir = source_dir
        self.pages = manifest["provider_query_pages"]
        self.next_page = 0

    def __call__(self, url: str, *, params: dict, timeout: int) -> _OfflineResponse:
        if url != URL or timeout != 30 or self.next_page >= len(self.pages):
            raise ArchiveReplayError("unrecorded provider request forbidden")
        record = self.pages[self.next_page]
        if params != record["query"]:
            raise ArchiveReplayError("provider request diverges from pinned chronology")
        name = f"{record['query']['symbol']}_mark_60m_page_{self.next_page:03d}.json"
        if record.get("provider_response_path") != f"provider_pages/{name}":
            raise ArchiveReplayError("untrusted provider response path")
        path = self.source_dir / "provider_pages" / name
        if not path.is_file() or path.is_symlink():
            raise ArchiveReplayError("missing or symlinked provider response page")
        if path.stat().st_size > 4_000_000:
            raise ArchiveReplayError("provider response page exceeds size ceiling")
        raw = path.read_bytes()
        if _sha(raw) != record.get("provider_response_sha256"):
            raise ArchiveReplayError("provider response SHA-256 mismatch")
        try:
            payload = json.loads(raw)
        except (UnicodeError, ValueError) as exc:
            raise ArchiveReplayError("invalid provider response JSON") from exc
        if not isinstance(payload, dict) or _canonical(payload) != raw:
            raise ArchiveReplayError("noncanonical provider response JSON")
        if _sha(_canonical(payload.get("result"))) != record.get("provider_result_sha256"):
            raise ArchiveReplayError("provider result SHA-256 mismatch")
        self.next_page += 1
        return _OfflineResponse(payload)


def replay_archived_symbol(
    *,
    archived_root: Path,
    funding_root: Path,
    output_root: Path,
    symbol: str,
    start: datetime,
    end: datetime,
    expected_manifest_sha256: str,
) -> dict:
    """Regenerate one pinned source into a NEW destination, atomically.

    A source captured with the previous collector is not independently
    authenticated by Bybit. The detached hash must be approved outside the
    archive; copying a hash from the archive itself is not sufficient.
    """
    if not re.fullmatch(r"[A-Z0-9]{3,20}", symbol):
        raise ArchiveReplayError("invalid instrument identifier")
    archived_root = Path(archived_root)
    funding_root = Path(funding_root)
    output_root = Path(output_root)
    if (
        archived_root.is_symlink()
        or funding_root.is_symlink()
        or output_root.is_symlink()
    ):
        raise ArchiveReplayError("symlinked archive or output roots forbidden")
    if output_root.resolve() == archived_root.resolve():
        raise ArchiveReplayError("output root must differ from source")
    source_dir = archived_root / symbol
    meta_file = source_dir / f"{symbol}_mark_60m.manifest.json"
    raw_manifest = _pinned_file(meta_file, expected_manifest_sha256)
    try:
        meta = json.loads(raw_manifest)
    except (UnicodeError, ValueError) as exc:
        raise ArchiveReplayError("invalid pinned source manifest") from exc
    if (
        not isinstance(meta, dict)
        or meta.get("schema_version") != "bybit-mark-price-hourly-sensitivity-v1"
        or meta.get("symbol") != symbol
        or meta.get("endpoint") != URL
        or meta.get("category") != "linear"
        or meta.get("interval_minutes") != 60
        or meta.get("start") != start.isoformat()
        or meta.get("end") != end.isoformat()
        or meta.get("provider_response_representation")
        != "CANONICAL_DECODED_JSON_NOT_HTTP_WIRE"
    ):
        raise ArchiveReplayError("pinned archive contract or fixed window mismatch")

    # Validate the *existing* source and independent original funding archive
    # before writing a new copy, including every response page and source CSV.
    try:
        verify_symbol_archive(
            root=archived_root, funding_root=funding_root,
            symbol=symbol, mark_manifest=meta,
        )
    except ValueError as exc:
        raise ArchiveReplayError(f"source archive rejected: {exc}") from exc

    target = output_root / symbol
    if target.exists() or target.is_symlink():
        raise ArchiveReplayError("destination symbol already exists; no overwrite")
    output_root.mkdir(parents=True, exist_ok=True)
    page_source = _PinnedPageReader(source_dir, meta)
    funding_csv = funding_root / f"{symbol}_funding.csv"
    funding_manifest = funding_root / f"{symbol}_funding.manifest.json"
    with tempfile.TemporaryDirectory(prefix=".astra-mark-replay-", dir=output_root) as tmp:
        staged_root = Path(tmp)
        staged_dir = staged_root / symbol
        reproduced = collect_symbol(
            symbol=symbol,
            start=start,
            end=end,
            funding_csv=funding_csv,
            funding_manifest=funding_manifest,
            output_dir=staged_dir,
            get=page_source,
            pause_seconds=0,
        )
        if page_source.next_page != len(page_source.pages):
            raise ArchiveReplayError("not all pinned response pages were replayed")
        regenerated = staged_dir / f"{symbol}_mark_60m.manifest.json"
        if _sha(regenerated.read_bytes()) != expected_manifest_sha256:
            raise ArchiveReplayError("replayed manifest differs from pinned source")
        try:
            verified = verify_symbol_archive(
                root=staged_root, funding_root=funding_root,
                symbol=symbol, mark_manifest=reproduced,
            )
        except ValueError as exc:
            raise ArchiveReplayError(f"replayed archive rejected: {exc}") from exc
        receipt = {
            "schema_version": "astra-offline-mark-price-replay-receipt-v1",
            "symbol": symbol,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "manifest_sha256": expected_manifest_sha256,
            "hourly_rows": verified["hourly_rows"],
            "funding_settlements": verified["funding_settlements"],
            "provider_pages": verified["provider_pages"],
            "source_hash_consistency_verified": True,
            "actual_provider_origin_verified": False,
            "account_funding_ledger_verified": False,
            "exact_settlement_mark_verified": False,
            "strategy_replay_completed": False,
            "strategy_promotion_allowed": False,
            "profitability_proven": False,
        }
        (staged_dir / "offline-replay-receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # Atomic publication: incomplete/corrupted replay leaves no admitted
        # symbol directory. Never overwrite a prior reviewed import.
        publish_new_directory(staged_dir, target)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archived-root", type=Path, required=True)
    parser.add_argument("--funding-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args()
    receipt = replay_archived_symbol(
        archived_root=args.archived_root,
        funding_root=args.funding_root,
        output_root=args.output_root,
        symbol=args.symbol,
        start=_utc(args.start_utc, "start"),
        end=_utc(args.end_utc, "end"),
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    print("OFFLINE_MARK_PRICE_REPLAY_RESEARCH_ONLY", json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
