from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from tools.product_identity import stable_identity_findings


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release(root: Path) -> dict[str, object]:
    identity_path = root / "RELEASE_IDENTITY_V109.json"
    if not identity_path.is_file():
        return {
            "schema": 109,
            "status": "FAIL",
            "files_verified": 0,
            "findings": ["missing:RELEASE_IDENTITY_V109.json"],
        }
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "schema": 109,
            "status": "FAIL",
            "files_verified": 0,
            "findings": ["invalid:RELEASE_IDENTITY_V109.json"],
        }

    findings: list[str] = []
    pyproject = root / "pyproject.toml"
    pyproject_text = pyproject.read_text(encoding="utf-8") if pyproject.is_file() else ""
    findings.extend(stable_identity_findings(pyproject_text, exact_version=(7, 39, 0)))
    if identity.get("schema") != 109 or identity.get("version") != "7.39.0":
        findings.append("identity:metadata")

    files = identity.get("files")
    if not isinstance(files, dict) or not files:
        findings.append("identity:files")
        files = {}
    verified = 0
    for relative, expected in sorted(files.items()):
        path = root / relative
        if not path.is_file():
            findings.append(f"missing:{relative}")
            continue
        verified += 1
        if not isinstance(expected, str) or _sha256(path) != expected:
            findings.append(f"digest:{relative}")

    return {
        "schema": 109,
        "version": identity.get("version"),
        "status": "PASS" if not findings else "FAIL",
        "files_verified": verified,
        "findings": findings,
    }


def live_status(root: Path) -> dict[str, object]:
    path = root / "LIVE_EXECUTION_STATUS_V109.json"
    if not path.is_file():
        return {"schema": 109, "status": "UNKNOWN", "findings": ["missing status file"]}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"schema": 109, "status": "UNKNOWN", "findings": ["invalid status file"]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-platform-v109")
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("live-status")
    sub.add_parser("verify-release")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    result = live_status(root) if args.command == "live-status" else verify_release(root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if args.command == "live-status" or result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
