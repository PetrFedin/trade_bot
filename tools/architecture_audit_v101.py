from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Sequence

REQUIRED = (
    "app/runtime/sandbox_qualification_v101.py",
    "app/runtime/alpaca_external_probe_v101.py",
    "app/runtime/sandbox_chaos_v101.py",
    "app/platform_assets/v101/migrations/001_external_sandbox_qualification.sql",
    "tools/platform_v101.py",
    "tools/static_audit_v101.py",
    "tools/stress_v101.py",
    "tests/test_sandbox_qualification_v101.py",
    "tests/test_alpaca_external_probe_v101.py",
    "tests/test_sandbox_chaos_v101.py",
    "ENGINEERING_REPORT_V101.md",
    "OPERATOR_RUNBOOK_V101.md",
    "LIVE_EXECUTION_STATUS_V101.json",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    for relative in REQUIRED:
        if not (root / relative).is_file():
            findings.append(f"missing:{relative}")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8") if (root / "pyproject.toml").exists() else ""
    name_match = re.search(r'^name\s*=\s*"astra-schema(?P<schema>\d+)[^"]*"\s*$', pyproject, re.MULTILINE)
    version_match = re.search(r'^version\s*=\s*"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"\s*$', pyproject, re.MULTILINE)
    if not name_match or int(name_match.group("schema")) < 101:
        findings.append("package_identity")
    if not version_match or tuple(int(version_match.group(part)) for part in ("major", "minor", "patch")) < (7, 31, 0):
        findings.append("package_version")
    runtime = (root / "app/runtime/sandbox_qualification_v101.py").read_text(encoding="utf-8") if (root / "app/runtime/sandbox_qualification_v101.py").exists() else ""
    for token in ("ApprovalReplay", "KILL_SWITCH_ENGAGED", "CLEANUP_VERIFIED", "external_order_routing_allowed", "live_trading_allowed"):
        if token not in runtime:
            findings.append(f"runtime_boundary:{token}")
    return {
        "schema": 101,
        "status": "PASS" if not findings else "FAIL",
        "required_files": len(REQUIRED),
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
