from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Sequence

REQUIRED = (
    "app/runtime/deployment_qualification_v106.py",
    "app/runtime/kubernetes_qualification_adapter_v106.py",
    "app/runtime/postgres_deployment_qualification_v106.py",
    "app/platform_assets/v106/migrations/001_production_fleet_deployment_qualification.sql",
    "migrations/v106/001_production_fleet_deployment_qualification.sql",
    "tools/platform_v106.py",
    "tools/architecture_audit_v106.py",
    "tools/static_audit_v106.py",
    "tools/stress_v106.py",
    "tests/test_deployment_qualification_v106.py",
    "tests/test_kubernetes_qualification_adapter_v106.py",
    "tests/test_postgres_deployment_qualification_v106.py",
    "tests/test_tools_v106.py",
    ".github/workflows/schema106-production-fleet-deployment-qualification.yml",
    "ENGINEERING_REPORT_V106.md",
    "OPERATOR_RUNBOOK_V106.md",
    "LIVE_EXECUTION_STATUS_V106.json",
    "RELEASE_IDENTITY_V106.json",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    for relative in REQUIRED:
        if not (root / relative).is_file():
            findings.append(f"missing:{relative}")

    def read(relative: str) -> str:
        path = root / relative
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    pyproject = read("pyproject.toml")
    name = re.search(r'^name\s*=\s*"astra-schema(?P<schema>\d+)[^"]*"$', pyproject, re.MULTILINE)
    version = re.search(r'^version\s*=\s*"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"$', pyproject, re.MULTILINE)
    if not name or int(name.group("schema")) < 106:
        findings.append("package_identity")
    if not version or tuple(int(version.group(part)) for part in ("major", "minor", "patch")) < (7, 36, 0):
        findings.append("package_version")
    runtime = read("app/runtime/deployment_qualification_v106.py")
    for token in (
        "SignedDeploymentManifestV106",
        "evaluate_preflight_v106",
        "RolloutActionV106",
        "CertificateRenewalDrillV106",
        "DisasterRecoveryDrillV106",
        "KUBERNETES_MUTATIONS_ALLOWED_V106 = False",
        "EXTERNAL_ORDER_ROUTING_ALLOWED_V106 = False",
        "LIVE_TRADING_ALLOWED_V106 = False",
    ):
        if token not in runtime:
            findings.append(f"runtime_boundary:{token}")
    adapter = read("app/runtime/kubernetes_qualification_adapter_v106.py")
    for token in ("method=\"GET\"", "tls_verify=True", "allow_redirects=False", "KUBERNETES_MUTATIONS_ALLOWED_V106 = False"):
        if token not in adapter:
            findings.append(f"kubernetes_boundary:{token}")
    migration = read("migrations/v106/001_production_fleet_deployment_qualification.sql")
    for token in ("FOR UPDATE SKIP LOCKED", "SECURITY DEFINER", "SET search_path", "REVOKE ALL", "qualification_event_append_only", "disaster_recovery_event_append_only"):
        if token not in migration:
            findings.append(f"migration_boundary:{token}")
    return {"schema": 106, "status": "PASS" if not findings else "FAIL", "required_files": len(REQUIRED), "findings": findings}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = audit(args.root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
