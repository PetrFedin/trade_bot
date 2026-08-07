from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence

from tools.product_identity import (
    STABLE_PACKAGE_NAME,
    compatible_schema_for_version,
    parse_product_identity,
    stable_identity_findings,
)

_SUCCESSOR_MUTABLE_FILES = frozenset(
    {
        "README.md",
        "pyproject.toml",
        "tools/architecture_audit_v106.py",
        "tools/platform_v106.py",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_identity(root: Path) -> tuple[int, tuple[int, int, int]]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return 0, (0, 0, 0)
    identity = parse_product_identity(path.read_text(encoding="utf-8"))
    if identity is None or identity.name != STABLE_PACKAGE_NAME:
        return 0, (0, 0, 0)
    return compatible_schema_for_version(identity.version), identity.version


def verify_release(root: Path) -> dict[str, object]:
    identity_path = root / "RELEASE_IDENTITY_V106.json"
    if not identity_path.is_file():
        return {
            "schema": 106,
            "status": "FAIL",
            "mode": "unknown",
            "files_checked": 0,
            "files_verified": 0,
            "files_ignored": [],
            "findings": ["missing:RELEASE_IDENTITY_V106.json"],
        }
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "schema": 106,
            "status": "FAIL",
            "mode": "unknown",
            "files_checked": 0,
            "files_verified": 0,
            "files_ignored": [],
            "findings": ["invalid:RELEASE_IDENTITY_V106.json"],
        }

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    current_schema, current_version = _package_identity(root)
    findings = list(stable_identity_findings(pyproject, minimum_version=(7, 36, 0)))
    successor = current_schema > 106
    ignored: list[str] = []
    checked = 0
    verified = 0
    files = identity.get("files", {})
    if not isinstance(files, dict) or not files:
        findings.append("identity:files")
        files = {}
    for relative, expected in sorted(files.items()):
        checked += 1
        path = root / relative
        if not path.is_file():
            findings.append(f"missing:{relative}")
            continue
        if successor and relative in _SUCCESSOR_MUTABLE_FILES:
            ignored.append(relative)
            continue
        verified += 1
        if not isinstance(expected, str) or _sha256(path) != expected:
            findings.append(f"digest:{relative}")
    return {
        "schema": 106,
        "status": "PASS" if not findings else "FAIL",
        "mode": "successor" if successor else "release",
        "current_schema": current_schema,
        "current_version": ".".join(str(part) for part in current_version),
        "files_checked": checked,
        "files_verified": verified,
        "files_ignored": ignored,
        "findings": findings,
    }


def live_status(root: Path) -> dict[str, object]:
    path = root / "LIVE_EXECUTION_STATUS_V106.json"
    if not path.is_file():
        return {"schema": 106, "status": "UNKNOWN", "findings": ["missing status file"]}
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-platform-v106")
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-release")
    sub.add_parser("live-status")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    result = verify_release(root) if args.command == "verify-release" else live_status(root)
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.command == "live-status":
        return 0
    return 0 if result.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
