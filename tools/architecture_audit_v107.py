from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from tools.product_identity import stable_identity_findings

REQUIRED = (
    "app/runtime/rollout_execution_v107.py",
    "app/runtime/kubernetes_rollout_adapter_v107.py",
    "app/runtime/postgres_rollout_repository_v107.py",
    "app/runtime/rollout_service_v107.py",
    "app/runtime/qualification_bridge_v107.py",
    "app/platform_assets/v107/migrations/001_production_rollout_actuator.sql",
    "migrations/v107/001_production_rollout_actuator.sql",
    "tools/platform_v107.py",
    "tools/architecture_audit_v107.py",
    "tools/static_audit_v107.py",
    "tools/stress_v107.py",
    "tests/test_rollout_execution_v107.py",
    "tests/test_kubernetes_rollout_adapter_v107.py",
    "tests/test_postgres_rollout_repository_v107.py",
    "tests/test_rollout_service_v107.py",
    "tests/test_qualification_bridge_v107.py",
    "tests/test_validation_edges_v107.py",
    "tests/test_tools_v107.py",
    ".github/workflows/schema107-production-rollout-actuator.yml",
    "ENGINEERING_REPORT_V107.md",
    "INTEGRATION_V107.md",
    "OPERATOR_RUNBOOK_V107.md",
    "QUALIFICATION_SUMMARY_V107.md",
    "RELEASE_NOTES_V107.md",
    "LIVE_EXECUTION_STATUS_V107.json",
    "RELEASE_IDENTITY_V107.json",
    "RELEASE_IDENTITY_V106.json",
    "tools/platform_v106.py",
    "pyproject.toml",
    "README.md",
    "tests/conftest.py",
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
    findings.extend(stable_identity_findings(pyproject, minimum_version=(7, 37, 0)))

    execution = read("app/runtime/rollout_execution_v107.py")
    for token in (
        "ApprovalRoleV107.RELEASE",
        "ApprovalRoleV107.RISK",
        "qualification_action_digest",
        "KUBERNETES_MUTATION_ATTEMPTS_V107 = 1",
        "EXTERNAL_ORDER_ROUTING_ALLOWED_V107 = False",
        "LIVE_TRADING_ALLOWED_V107 = False",
        "certify_full_rollout_v107",
    ):
        if token not in execution:
            findings.append(f"execution_boundary:{token}")

    adapter = read("app/runtime/kubernetes_rollout_adapter_v107.py")
    for token in (
        'method="GET"',
        'method="PATCH"',
        "tls_verify=True",
        "allow_redirects=False",
        "application/json-patch+json",
        "KUBERNETES_MUTATION_ATTEMPTS_V107 = 1",
    ):
        if token not in adapter:
            findings.append(f"kubernetes_boundary:{token}")

    repository = read("app/runtime/postgres_rollout_repository_v107.py")
    for token in (
        "FOR UPDATE SKIP LOCKED",
        "astra_rollout_fence_v107.fencing_token < EXCLUDED.fencing_token",
        "deployment_uid, fencing_token",
        "MUTATION_STARTED",
        "VERIFYING",
        "UNCERTAIN",
        "mutation_attempts = 0",
    ):
        if token not in repository:
            findings.append(f"repository_boundary:{token}")

    service = read("app/runtime/rollout_service_v107.py")
    for token in (
        "Re-verify immediately before the durable mutation marker",
        "PATCH outcome is ambiguous; recovery is GET-only",
        "RECOVERY_MUTATION_ALLOWED_V107 = False",
        "KUBERNETES_PATCH_RETRY_ALLOWED_V107 = False",
    ):
        if token not in service:
            findings.append(f"service_boundary:{token}")

    migration = read("migrations/v107/001_production_rollout_actuator.sql")
    for token in (
        "astra_rollout_fence_v107",
        "astra_rollout_event_append_only_v107",
        "REVOKE ALL",
        "mutation_attempts IN (0, 1)",
        "state IN ('MUTATION_STARTED', 'VERIFYING', 'UNCERTAIN')",
    ):
        if token not in migration:
            findings.append(f"migration_boundary:{token}")

    return {
        "schema": 107,
        "status": "PASS" if not findings else "FAIL",
        "required_files": len(REQUIRED),
        "findings": findings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-architecture-audit-v107")
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = audit(args.root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
