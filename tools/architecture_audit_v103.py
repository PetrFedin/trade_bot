from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from tools.product_identity import stable_identity_findings

REQUIRED = (
    "app/runtime/campaign_control_plane_v103.py",
    "app/runtime/postgres_control_plane_v103.py",
    "app/platform_assets/v103/migrations/001_production_campaign_control_plane.sql",
    "migrations/v103/001_production_campaign_control_plane.sql",
    "tools/platform_v103.py",
    "tools/static_audit_v103.py",
    "tools/stress_v103.py",
    "tests/test_campaign_control_plane_v103.py",
    "tests/test_postgres_control_plane_v103.py",
    "tests/test_tools_v103.py",
    ".github/workflows/schema103-production-control-plane.yml",
    "ENGINEERING_REPORT_V103.md",
    "OPERATOR_RUNBOOK_V103.md",
    "LIVE_EXECUTION_STATUS_V103.json",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    for relative in REQUIRED:
        if not (root / relative).is_file():
            findings.append(f"missing:{relative}")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    findings.extend(stable_identity_findings(pyproject, minimum_version=(7, 33, 0)))
    runtime = (root / "app/runtime/campaign_control_plane_v103.py").read_text(
        encoding="utf-8"
    )
    for token in (
        "fencing_token",
        "heartbeat_ttl",
        "ReadOnlyProbePlanV103",
        "EvidenceManifestV103",
        "retention_sweep",
        "INCIDENT_BUDGET_EXHAUSTED",
        "external_order_routing_allowed",
        "live_trading_allowed",
    ):
        if token not in runtime:
            findings.append(f"runtime_boundary:{token}")
    postgres = (root / "app/runtime/postgres_control_plane_v103.py").read_text(
        encoding="utf-8"
    )
    for token in (
        "FOR UPDATE SKIP LOCKED",
        "claim_campaign_lease",
        "record_worker_heartbeat",
    ):
        if token not in postgres:
            findings.append(f"postgres_boundary:{token}")
    migration = (root / "migrations/v103/001_production_campaign_control_plane.sql").read_text(
        encoding="utf-8"
    )
    for token in (
        "control_plane_event_append_only",
        "evidence_chunk_append_only",
        "worker_heartbeat_append_only",
        "SECURITY DEFINER",
        "REVOKE ALL",
    ):
        if token not in migration:
            findings.append(f"migration_boundary:{token}")
    return {
        "schema": 103,
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
