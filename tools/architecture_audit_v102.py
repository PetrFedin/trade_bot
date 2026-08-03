from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Sequence

REQUIRED = (
    "app/runtime/sandbox_soak_orchestrator_v102.py",
    "app/platform_assets/v102/migrations/001_sandbox_soak_orchestrator.sql",
    "migrations/v102/001_sandbox_soak_orchestrator.sql",
    "tools/platform_v102.py",
    "tools/static_audit_v102.py",
    "tools/stress_v102.py",
    "tests/test_sandbox_soak_orchestrator_v102.py",
    "tests/test_tools_v102.py",
    ".github/workflows/schema102-sandbox-soak-orchestrator.yml",
    "ENGINEERING_REPORT_V102.md",
    "OPERATOR_RUNBOOK_V102.md",
    "LIVE_EXECUTION_STATUS_V102.json",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    for relative in REQUIRED:
        if not (root / relative).is_file():
            findings.append(f"missing:{relative}")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    name = re.search(r'^name\s*=\s*"astra-schema(?P<schema>\d+)[^"]*"$', pyproject, re.MULTILINE)
    version = re.search(r'^version\s*=\s*"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"$', pyproject, re.MULTILINE)
    if not name or int(name.group("schema")) < 102:
        findings.append("package_identity")
    if not version or tuple(int(version.group(part)) for part in ("major", "minor", "patch")) < (7, 32, 0):
        findings.append("package_version")
    runtime = (root / "app/runtime/sandbox_soak_orchestrator_v102.py").read_text(encoding="utf-8")
    for token in (
        "fencing_token",
        "evidence_retention",
        "TOTAL_FAILURE_BUDGET_EXHAUSTED",
        "CONSECUTIVE_FAILURE_BUDGET_EXHAUSTED",
        "RESIDUAL_PAPER_EXPOSURE",
        "eligible_for_extended_paper_soak",
        "external_order_routing_allowed",
        "live_trading_allowed",
    ):
        if token not in runtime:
            findings.append(f"runtime_boundary:{token}")
    migration = (root / "migrations/v102/001_sandbox_soak_orchestrator.sql").read_text(encoding="utf-8")
    for token in ("soak_campaign_event_append_only", "soak_run_evidence_append_only", "REVOKE ALL"):
        if token not in migration:
            findings.append(f"migration_boundary:{token}")
    return {
        "schema": 102,
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
