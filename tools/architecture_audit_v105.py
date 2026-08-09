from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Sequence

REQUIRED = (
    "app/runtime/fleet_operations_v105.py",
    "app/runtime/s3_evidence_adapter_v105.py",
    "app/runtime/postgres_fleet_operations_v105.py",
    "app/platform_assets/v105/migrations/001_production_worker_fleet_operations.sql",
    "migrations/v105/001_production_worker_fleet_operations.sql",
    "tools/platform_v105.py",
    "tools/static_audit_v105.py",
    "tools/stress_v105.py",
    "tests/test_fleet_operations_v105.py",
    "tests/test_s3_evidence_adapter_v105.py",
    "tests/test_postgres_fleet_operations_v105.py",
    "tests/test_tools_v105.py",
    ".github/workflows/schema105-production-worker-fleet-operations.yml",
    "ENGINEERING_REPORT_V105.md",
    "OPERATOR_RUNBOOK_V105.md",
    "LIVE_EXECUTION_STATUS_V105.json",
    "RELEASE_IDENTITY_V105.json",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    for relative in REQUIRED:
        if not (root / relative).is_file():
            findings.append(f"missing:{relative}")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    name = re.search(r'^name\s*=\s*"astra-schema(?P<schema>\d+)[^"]*"$', pyproject, re.MULTILINE)
    version = re.search(r'^version\s*=\s*"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"$', pyproject, re.MULTILINE)
    if not name or int(name.group("schema")) < 105:
        findings.append("package_identity")
    if not version or tuple(int(version.group(part)) for part in ("major", "minor", "patch")) < (7, 35, 0):
        findings.append("package_version")

    runtime = (root / "app/runtime/fleet_operations_v105.py").read_text(encoding="utf-8")
    for token in (
        "SignedEnrollmentV105", "RotatingKeyRingV105", "KubernetesAttestationV105",
        "ControlledAutoscalerV105", "DRAIN_TIMEOUT_QUARANTINE", "CONTAINMENT_ACTIVATED",
        "external_order_routing_allowed", "live_trading_allowed",
    ):
        if token not in runtime:
            findings.append(f"runtime_boundary:{token}")

    s3 = (root / "app/runtime/s3_evidence_adapter_v105.py").read_text(encoding="utf-8")
    for token in ("tls_verify: bool = True", "allow_redirects: bool = False", "AmbiguousS3MutationV105", "x-amz-checksum-sha256", "_verify_head"):
        if token not in s3:
            findings.append(f"s3_boundary:{token}")

    postgres = (root / "app/runtime/postgres_fleet_operations_v105.py").read_text(encoding="utf-8")
    for token in ("FOR UPDATE SKIP LOCKED", "heartbeat fencing rejected", "rollback"):
        if token not in postgres:
            findings.append(f"postgres_boundary:{token}")

    migration = (root / "migrations/v105/001_production_worker_fleet_operations.sql").read_text(encoding="utf-8")
    for token in ("SECURITY DEFINER", "FOR UPDATE SKIP LOCKED", "reject_append_only_mutation", "REVOKE ALL"):
        if token not in migration:
            findings.append(f"migration_boundary:{token}")

    return {"schema": 105, "status": "PASS" if not findings else "FAIL", "required_files": len(REQUIRED), "findings": findings}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = audit(args.root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
