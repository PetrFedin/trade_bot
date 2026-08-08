from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
import zlib


@dataclass(frozen=True)
class ArchiveCandidate:
    mode: str
    encoded: str
    archive: bytes
    members: tuple[dict[str, object], ...]
    metadata: dict[str, object]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_base64(text: str) -> str | None:
    """Remove misplaced padding and restore only legal terminal padding."""

    body = text.replace("=", "")
    remainder = len(body) % 4
    if remainder == 1:
        return None
    return body + ("=" * ((4 - remainder) % 4))


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or str(path) != name:
        raise ValueError(f"unsafe archive path: {name}")
    return path


def _inspect_archive(encoded: str) -> tuple[bytes, tuple[dict[str, object], ...]]:
    archive = base64.b64decode(encoded, validate=True)
    tar_bytes = gzip.decompress(archive)
    members: list[dict[str, object]] = []
    names: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as handle:
        for member in handle.getmembers():
            path = _safe_member_path(member.name)
            if member.name in names:
                raise ValueError(f"duplicate archive member: {member.name}")
            names.add(member.name)
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError(f"unsupported archive member type: {member.name}")
            source = handle.extractfile(member)
            if source is None:
                raise ValueError(f"unreadable archive member: {member.name}")
            body = source.read()
            members.append(
                {
                    "path": str(path),
                    "size": len(body),
                    "sha256": _sha256(body),
                }
            )
    if not members:
        raise ValueError("empty archive")
    return archive, tuple(members)


def _tar_header_checksum_valid(header: bytes) -> bool:
    if len(header) != 512:
        return False
    raw = header[148:156].rstrip(b"\0 ").strip()
    try:
        expected = int(raw or b"0", 8)
    except ValueError:
        return False
    normalized = header[:148] + (b" " * 8) + header[156:]
    return sum(normalized) == expected


def _scan_partial_tar(data: bytes) -> tuple[list[dict[str, object]], list[tuple[str, bytes]]]:
    members: list[dict[str, object]] = []
    complete_files: list[tuple[str, bytes]] = []
    position = 0
    while position + 512 <= len(data):
        header = data[position : position + 512]
        if header == b"\0" * 512:
            break
        if not _tar_header_checksum_valid(header):
            break
        raw_name = header[:100].split(b"\0", 1)[0]
        try:
            name = raw_name.decode("utf-8")
            _safe_member_path(name)
        except (UnicodeDecodeError, ValueError):
            break
        raw_size = header[124:136].split(b"\0", 1)[0].strip() or b"0"
        try:
            size = int(raw_size, 8)
        except ValueError:
            break
        typeflag = header[156:157]
        if typeflag not in (b"0", b"\0"):
            break
        data_start = position + 512
        data_end = data_start + size
        if data_end > len(data):
            members.append({"path": name, "size": size, "complete": False})
            break
        body = data[data_start:data_end]
        members.append(
            {
                "path": name,
                "size": size,
                "complete": True,
                "sha256": _sha256(body),
            }
        )
        complete_files.append((name, body))
        position = data_start + ((size + 511) // 512) * 512
    return members, complete_files


def _stream_probe(encoded: str, *, normalize_padding: bool) -> tuple[dict[str, object], list[tuple[str, bytes]]]:
    source = encoded.replace("=", "") if normalize_padding else encoded.split("=", 1)[0]
    usable_chars = len(source) - (len(source) % 4)
    source = source[:usable_chars]
    result: dict[str, object] = {
        "normalize_padding": normalize_padding,
        "encoded_chars_probed": usable_chars,
        "encoded_length_mod4": len(encoded) % 4,
        "gzip_header": False,
        "gzip_eof": False,
        "compressed_bytes_decoded": 0,
        "compressed_failure_offset": None,
        "approx_encoded_failure_offset": None,
        "zlib_error": None,
        "partial_tar_members": [],
        "uncompressed_bytes_before_failure": 0,
    }
    try:
        compressed = base64.b64decode(source, validate=True)
    except binascii.Error as exc:
        result["zlib_error"] = f"base64:{exc}"
        return result, []
    result["compressed_bytes_decoded"] = len(compressed)
    result["gzip_header"] = compressed.startswith(b"\x1f\x8b")
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = bytearray()
    step = 32
    offset = 0
    while offset < len(compressed):
        block = compressed[offset : offset + step]
        try:
            output.extend(decompressor.decompress(block))
        except zlib.error as exc:
            result["compressed_failure_offset"] = offset
            result["approx_encoded_failure_offset"] = (offset * 4) // 3
            result["zlib_error"] = str(exc)
            break
        offset += len(block)
    else:
        result["gzip_eof"] = decompressor.eof
        if decompressor.eof:
            output.extend(decompressor.flush())
    result["uncompressed_bytes_before_failure"] = len(output)
    partial_members, complete_files = _scan_partial_tar(bytes(output))
    result["partial_tar_members"] = partial_members
    return result, complete_files


def _longest_suffix_prefix(left: str, right: str, maximum: int = 2048) -> int:
    limit = min(len(left), len(right), maximum)
    for size in range(limit, 0, -1):
        if left[-size:] == right[:size]:
            return size
    return 0


def _longest_suffix_suffix(left: str, right: str, maximum: int = 2048) -> int:
    left_body = left.rstrip("=")
    right_body = right.rstrip("=")
    limit = min(len(left_body), len(right_body), maximum)
    for size in range(limit, 0, -1):
        if left_body[-size:] == right_body[-size:]:
            return size
    return 0


def _padding_positions(text: str) -> list[int]:
    return [index for index, character in enumerate(text) if character == "="]


def _candidate(
    mode: str,
    raw_encoded: str,
    metadata: dict[str, object],
) -> ArchiveCandidate | None:
    encoded = _canonical_base64(raw_encoded)
    if encoded is None:
        return None
    try:
        archive, members = _inspect_archive(encoded)
    except Exception:
        return None
    return ArchiveCandidate(mode, encoded, archive, members, metadata)


def _recover(encoded_parts: Sequence[str]) -> tuple[ArchiveCandidate | None, list[dict[str, object]]]:
    valid: dict[str, ArchiveCandidate] = {}
    attempts: list[dict[str, object]] = []

    def consider(mode: str, raw: str, metadata: dict[str, object]) -> None:
        candidate = _candidate(mode, raw, metadata)
        attempts.append(
            {
                "mode": mode,
                "raw_length": len(raw),
                "raw_length_mod4": len(raw) % 4,
                "valid": candidate is not None,
                **metadata,
            }
        )
        if candidate is None:
            return
        archive_digest = _sha256(candidate.archive)
        valid.setdefault(archive_digest, candidate)

    original = "".join(encoded_parts)
    consider("direct", original, {})

    if len(encoded_parts) >= 2:
        # The final staged chunk is a repair-tail candidate, so first test the
        # pre-repair payload with illegal embedded padding removed.
        consider(
            "normalized-pre-repair",
            "".join(encoded_parts[:-1]).replace("=", ""),
            {"ignored_final_chunk": True},
        )
        consider(
            "normalized-all-chunks",
            original.replace("=", ""),
            {"ignored_final_chunk": False},
        )

        penultimate = encoded_parts[-2].replace("=", "")
        repair_tail = encoded_parts[-1].replace("=", "")
        fixed_prefix = "".join(encoded_parts[:-2]).replace("=", "")
        overlap = _longest_suffix_suffix(encoded_parts[-2], encoded_parts[-1])
        tail_valid_count_before = len(valid)
        for cut in range(len(penultimate) + 1):
            raw = fixed_prefix + penultimate[:cut] + repair_tail
            candidate = _candidate(
                "normalized-repair-tail",
                raw,
                {"cut": cut, "repair_overlap": overlap},
            )
            if candidate is None:
                continue
            valid.setdefault(_sha256(candidate.archive), candidate)
        attempts.append(
            {
                "mode": "normalized-repair-tail-search",
                "cuts_tested": len(penultimate) + 1,
                "repair_overlap": overlap,
                "new_unique_valid_archives": len(valid) - tail_valid_count_before,
            }
        )

    if len(valid) != 1:
        return None, attempts
    return next(iter(valid.values())), attempts


def _write_archive(candidate: ArchiveCandidate, root: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(candidate.archive), mode="r:gz") as handle:
        for member in handle.getmembers():
            path = _safe_member_path(member.name)
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError(f"unsupported archive member type: {member.name}")
            source = handle.extractfile(member)
            if source is None:
                raise ValueError(f"unreadable archive member: {member.name}")
            destination = (root / Path(*path.parts)).resolve()
            if destination != root and root not in destination.parents:
                raise ValueError(f"archive path escaped repository: {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read())


def _write_salvage(files: Sequence[tuple[str, bytes]], root: Path) -> None:
    for name, body in files:
        path = _safe_member_path(name)
        destination = (root / Path(*path.parts)).resolve()
        if destination != root and root not in destination.parents:
            raise ValueError(f"salvage path escaped root: {name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)


def analyze(
    payload_dir: Path,
    report_path: Path,
    *,
    extract_root: Path | None = None,
    salvage_root: Path | None = None,
) -> dict[str, object]:
    parts = sorted(payload_dir.glob("chunk-*.txt"))
    expected_names = [f"chunk-{index:03d}.txt" for index in range(len(parts))]
    actual_names = [path.name for path in parts]
    if not parts or actual_names != expected_names:
        report: dict[str, object] = {
            "status": "FAIL",
            "error": "missing or non-contiguous payload chunks",
            "actual_names": actual_names,
            "expected_names": expected_names,
        }
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return report

    encoded_parts: list[str] = []
    chunk_sha256: dict[str, str] = {}
    chunk_lengths: dict[str, int] = {}
    padding_positions: dict[str, list[int]] = {}
    validation_errors: list[str] = []
    for path in parts:
        data = path.read_bytes()
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError:
            validation_errors.append(f"non-ASCII:{path.name}")
            text = ""
        if any(character.isspace() for character in text):
            validation_errors.append(f"whitespace:{path.name}")
        chunk_sha256[path.name] = _sha256(data)
        chunk_lengths[path.name] = len(text)
        padding_positions[path.name] = _padding_positions(text)
        encoded_parts.append(text)

    boundary_forensics: list[dict[str, object]] = []
    for index in range(len(encoded_parts) - 1):
        left = encoded_parts[index]
        right = encoded_parts[index + 1]
        boundary_forensics.append(
            {
                "left": parts[index].name,
                "right": parts[index + 1].name,
                "left_length_mod4": len(left) % 4,
                "right_length_mod4": len(right) % 4,
                "suffix_prefix_overlap": _longest_suffix_prefix(left, right),
                "suffix_suffix_overlap": _longest_suffix_suffix(left, right),
            }
        )

    cumulative_probes: list[dict[str, object]] = []
    best_salvage: list[tuple[str, bytes]] = []
    best_salvage_bytes = -1
    cumulative = ""
    for index, part in enumerate(encoded_parts[:-1] or encoded_parts):
        cumulative += part
        for normalize in (False, True):
            probe, salvage = _stream_probe(cumulative, normalize_padding=normalize)
            probe["through_chunk"] = parts[index].name
            cumulative_probes.append(probe)
            uncompressed = int(probe["uncompressed_bytes_before_failure"])
            if uncompressed > best_salvage_bytes and salvage:
                best_salvage_bytes = uncompressed
                best_salvage = salvage

    candidate = None
    recovery_attempts: list[dict[str, object]] = []
    if not validation_errors:
        candidate, recovery_attempts = _recover(encoded_parts)

    recovered = candidate is not None
    original_encoded = "".join(encoded_parts)
    report = {
        "status": "PASS" if recovered else "FAIL",
        "chunk_count": len(parts),
        "chunk_lengths": chunk_lengths,
        "chunk_sha256": chunk_sha256,
        "padding_positions": padding_positions,
        "combined_length": len(original_encoded),
        "combined_length_mod4": len(original_encoded) % 4,
        "original_combined_base64_sha256": _sha256(original_encoded.encode("ascii")),
        "validation_errors": validation_errors,
        "boundary_forensics": boundary_forensics,
        "cumulative_gzip_probes": cumulative_probes,
        "recovery_attempts": recovery_attempts,
        "salvaged_complete_members": [
            {"path": name, "size": len(body), "sha256": _sha256(body)}
            for name, body in best_salvage
        ],
    }

    if salvage_root is not None and best_salvage:
        _write_salvage(best_salvage, salvage_root.resolve())

    if candidate is not None:
        report["recovery"] = {
            "mode": candidate.mode,
            "metadata": candidate.metadata,
            "selected_base64_sha256": _sha256(candidate.encoded.encode("ascii")),
            "archive_sha256": _sha256(candidate.archive),
            "archive_size": len(candidate.archive),
            "member_count": len(candidate.members),
            "members": list(candidate.members),
        }
        if extract_root is not None:
            _write_archive(candidate, extract_root.resolve())
    else:
        report["recovery"] = {"mode": "unrecovered"}

    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _write_github_output(recovered: bool) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write(f"recovered={'true' if recovered else 'false'}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="schema109-payload-forensics")
    parser.add_argument("--payload-dir", type=Path, default=Path("_schema109_payload"))
    parser.add_argument("--report", type=Path, default=Path("schema109-payload-report.json"))
    parser.add_argument("--extract-root", type=Path)
    parser.add_argument("--salvage-root", type=Path)
    args = parser.parse_args(argv)
    report = analyze(
        args.payload_dir.resolve(),
        args.report.resolve(),
        extract_root=args.extract_root,
        salvage_root=args.salvage_root,
    )
    recovered = report.get("status") == "PASS"
    _write_github_output(recovered)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
