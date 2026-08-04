from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release(root: Path) -> dict[str, object]:
    identity_path = root / "RELEASE_IDENTITY_V106.json"
    if not identity_path.is_file():
        return {"schema": 106, "status": "FAIL", "findings": ["missing:RELEASE_IDENTITY_V106.json"]}
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    findings: list[str] = []
    for relative, expected in sorted(identity.get("files", {}).items()):
        path = root / relative
        if not path.is_file():
            findings.append(f"missing:{relative}")
        elif _sha256(path) != expected:
            findings.append(f"digest:{relative}")
    return {
        "schema": 106,
        "version": identity.get("version"),
        "status": "PASS" if not findings else "FAIL",
        "files_verified": len(identity.get("files", {})),
        "findings": findings,
    }


def live_status(root: Path) -> dict[str, object]:
    path = root / "LIVE_EXECUTION_STATUS_V106.json"
    if not path.is_file():
        return {"schema": 106, "status": "UNKNOWN", "findings": ["missing status file"]}
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("live-status")
    sub.add_parser("verify-release")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "live-status":
        result = live_status(root)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    result = verify_release(root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
