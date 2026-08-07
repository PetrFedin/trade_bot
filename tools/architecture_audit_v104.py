from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from tools.product_identity import stable_identity_findings

REQUIRED = (
    "app/runtime/worker_execution_plane_v104.py",
    "app/runtime/postgres_worker_plane_v104.py",
    "migrations/v104/001_production_worker_execution_plane.sql",
    "tools/platform_v104.py",
    "tools/static_audit_v104.py",
    "tools/stress_v104.py",
    "tests/test_worker_execution_plane_v104.py",
    "tests/test_postgres_worker_plane_v104.py",
    ".github/workflows/schema104-production-worker-execution-plane.yml",
    "ENGINEERING_REPORT_V104.md",
    "OPERATOR_RUNBOOK_V104.md",
    "LIVE_EXECUTION_STATUS_V104.json",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    for item in REQUIRED:
        if not (root / item).is_file():
            findings.append(f"missing:{item}")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    findings.extend(stable_identity_findings(pyproject, minimum_version=(7, 34, 0)))
    runtime = (root / "app/runtime/worker_execution_plane_v104.py").read_text(
        encoding="utf-8"
    )
    for token in (
        "SignedWorkClaimV104",
        "WorkerAttestationV104",
        "ReplayLedgerV104",
        "EvidenceSpoolV104",
        "ResumableUploaderV104",
        "DeadLetterQueueV104",
        "PAPER_REST_BASE",
        "mutations are prohibited",
        "external_order_routing_allowed",
        "live_trading_allowed",
    ):
        if token not in runtime:
            findings.append(f"runtime_boundary:{token}")
    sql = (root / "migrations/v104/001_production_worker_execution_plane.sql").read_text(
        encoding="utf-8"
    )
    for token in (
        "FOR UPDATE SKIP LOCKED",
        "worker_event_append_only",
        "worker_dead_letter_append_only",
        "REVOKE ALL",
    ):
        if token not in sql:
            findings.append(f"migration_boundary:{token}")
    return {
        "schema": 104,
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
