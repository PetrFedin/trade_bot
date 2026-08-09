from __future__ import annotations

import argparse
import json
from pathlib import Path
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
    if 'name = "astra-schema101-external-sandbox-qualification"' not in pyproject:
        findings.append("package_identity")
    if 'version = "7.31.0"' not in pyproject:
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
