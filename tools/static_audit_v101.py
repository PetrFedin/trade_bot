from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

FORBIDDEN = (
    "verify=False",
    "verify = False",
    "ssl.CERT_NONE",
    "check_hostname = False",
    "_create_unverified_context",
    "https://api.alpaca.markets",
    "wss://api.alpaca.markets",
    "live_trading_allowed=True",
    "external_order_routing_allowed=True",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    files = sorted((root / "app").rglob("*.py")) + sorted(
        (root / "tools").rglob("*.py")
    )
    for path in files:
        # Static auditors intentionally contain the forbidden signatures they
        # search for. Excluding every versioned static auditor prevents the
        # scanner from flagging its own detection vocabulary while keeping all
        # runtime, CLI, migration helper and stress code in scope.
        if path.name.startswith("static_audit_v"):
            continue
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                findings.append(f"{path.relative_to(root)}:{token}")
    return {
        "schema": 101,
        "status": "PASS" if not findings else "FAIL",
        "python_files_checked": len(files),
        "findings": findings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = audit(args.root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
