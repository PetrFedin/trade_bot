from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from tools.product_identity import stable_identity_findings

REQUIRED = (
    "app/runtime/remote_signer_attestation_v109.py",
    "app/runtime/postgres_remote_signer_repository_v109.py",
    "app/platform_assets/v109/__init__.py",
    "app/platform_assets/v109/migrations/001_remote_signer_attestation.sql",
    "migrations/v109/001_remote_signer_attestation.sql",
    "tests/test_remote_signer_attestation_v109.py",
    "tests/test_postgres_remote_signer_repository_v109.py",
    "tests/test_postgres_remote_signer_repository_unit_v109.py",
    "tests/test_tools_v109.py",
    "tests/test_product_identity.py",
    "tools/platform_v109.py",
    "tools/architecture_audit_v109.py",
    "tools/static_audit_v109.py",
    "tools/stress_v109.py",
    "tools/platform_v108.py",
    "tools/architecture_audit_v108.py",
    "tools/product_identity.py",
    ".github/workflows/schema109-remote-signer-attestation.yml",
    "ENGINEERING_REPORT_V109.md",
    "INTEGRATION_V109.md",
    "OPERATOR_RUNBOOK_V109.md",
    "QUALIFICATION_SUMMARY_V109.md",
    "RELEASE_NOTES_V109.md",
    "LIVE_EXECUTION_STATUS_V109.json",
    "RELEASE_IDENTITY_V108.json",
    "RELEASE_IDENTITY_V109.json",
    "pyproject.toml",
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
    findings.extend(stable_identity_findings(pyproject, exact_version=(7, 39, 0)))
    if '"cryptography>=50,<51"' not in pyproject:
        findings.append("ed25519_dependency")

    runtime = read("app/runtime/remote_signer_attestation_v109.py")
    for token in (
        "class RemoteSignerPolicySnapshotV109",
        "verify_remote_signer_policy_v109",
        "endpoint must be an exact HTTPS origin",
        "ssl.TLSVersion.TLSv1_3",
        "class _NoRedirectV109",
        'self._request("POST", "/v1/signing/requests", body)',
        'self._request("GET", f"/v1/signing/requests/{quote(request_id, safe=\'\')}")',
        "self._repository.mark_dispatch_started(",
        "remote signer transport outcome is ambiguous",
        "hardware_signing_counter",
        "audit_chain_root",
        "Ed25519PublicKey",
    ):
        if token not in runtime:
            findings.append(f"runtime_boundary:{token}")

    repository = read("app/runtime/postgres_remote_signer_repository_v109.py")
    for token in (
        "connection.rollback()",
        "astra_remote_sign_outbox_v109",
        "state = 'DISPATCH_STARTED'",
        "audit checkpoint compare-and-set rejected",
        "checkpoint.audit_sequence < EXCLUDED.audit_sequence",
        "checkpoint.hardware_signing_counter < EXCLUDED.hardware_signing_counter",
    ):
        if token not in repository:
            findings.append(f"repository_boundary:{token}")

    migration = read("migrations/v109/001_remote_signer_attestation.sql")
    for token in (
        "astra_remote_sign_request_policy_fk_v109",
        "astra_remote_sign_outbox_v109",
        "astra_remote_sign_checkpoint_v109",
        "astra_remote_sign_event_append_only_v109",
        "BEFORE UPDATE OR DELETE",
        "REVOKE ALL",
    ):
        if token not in migration:
            findings.append(f"migration_boundary:{token}")

    status_text = read("LIVE_EXECUTION_STATUS_V109.json")
    if status_text:
        try:
            status = json.loads(status_text)
        except json.JSONDecodeError:
            findings.append("status:invalid_json")
        else:
            for key in (
                "production_remote_signer_verified",
                "production_signing_authority_verified",
                "production_kubernetes_mutation_authorized",
                "external_order_routing_allowed",
                "live_trading_allowed",
                "automatic_sign_post_retry_allowed",
                "private_key_material_persisted_by_runtime",
            ):
                if status.get(key) is not False:
                    findings.append(f"status_boundary:{key}")

    return {
        "schema": 109,
        "status": "PASS" if not findings else "FAIL",
        "required_files": len(REQUIRED),
        "findings": findings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-architecture-audit-v109")
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = audit(args.root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
