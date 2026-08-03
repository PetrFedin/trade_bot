from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence


def verify_release(root: Path) -> dict[str, object]:
    identity = json.loads((root / "RELEASE_IDENTITY_V105.json").read_text(encoding="utf-8"))
    findings: list[str] = []
    for relative, expected in identity.get("files", {}).items():
        path = root / relative
        if not path.is_file():
            findings.append(f"missing:{relative}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            findings.append(f"digest:{relative}")
    return {"schema": 105, "status": "PASS" if not findings else "FAIL", "files_checked": len(identity.get("files", {})), "findings": findings}


def live_status(root: Path) -> dict[str, object]:
    return json.loads((root / "LIVE_EXECUTION_STATUS_V105.json").read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-platform-v105")
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
