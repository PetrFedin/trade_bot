from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from tools.product_identity import stable_identity_findings

REQUIRED = (
    "app/runtime/signing_authority_v108.py",
    "app/runtime/rollout_crypto_bridge_v108.py",
    "app/runtime/postgres_signing_repository_v108.py",
    "app/platform_assets/v108/__init__.py",
    "app/platform_assets/v108/migrations/001_asymmetric_signing_authority.sql",
    "migrations/v108/001_asymmetric_signing_authority.sql",
    "tests/helpers_v108.py",
    "tests/test_signing_authority_v108.py",
    "tests/test_rollout_crypto_bridge_v108.py",
    "tests/test_postgres_signing_repository_v108.py",
    "tests/test_migration_contract_v108.py",
    "tests/test_tools_v108.py",
    "tools/platform_v108.py",
    "tools/architecture_audit_v108.py",
    "tools/static_audit_v108.py",
    "tools/stress_v108.py",
    ".github/workflows/schema108-asymmetric-signing-authority.yml",
    "ENGINEERING_REPORT_V108.md",
    "INTEGRATION_V108.md",
    "OPERATOR_RUNBOOK_V108.md",
    "QUALIFICATION_SUMMARY_V108.md",
    "RELEASE_NOTES_V108.md",
    "LIVE_EXECUTION_STATUS_V108.json",
    "RELEASE_IDENTITY_V108.json",
    "RELEASE_IDENTITY_V107.json",
    "tools/platform_v107.py",
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
    findings.extend(stable_identity_findings(pyproject, exact_version=(7, 38, 0)))
    if '"cryptography>=50,<51"' not in pyproject:
        findings.append("ed25519_dependency")

    authority = read("app/runtime/signing_authority_v108.py")
    for token in (
        "class Ed25519SigningProviderV108",
        "Ed25519PublicKey",
        "RootSignedKeyringSnapshotV108",
        "keyring generation is not monotonic",
        "authorization requires distinct owners",
        "SignatureReplayLedgerV108",
        "EXECUTOR_RECEIPT",
        "receipt_payload_digest_v108",
    ):
        if token not in authority:
            findings.append(f"authority_boundary:{token}")

    bridge = read("app/runtime/rollout_crypto_bridge_v108.py")
    for token in (
        "command.verify(",
        "verify_keyring_snapshot_v108",
        "verify_rollout_authorization_v108",
        "V107 HMAC command is treated only as a predecessor compatibility gate",
        "verify_v107_rollout_receipt_v108",
    ):
        if token not in bridge:
            findings.append(f"bridge_boundary:{token}")

    repository = read("app/runtime/postgres_signing_repository_v108.py")
    for token in (
        "astra_signing_keyring_v108.generation < EXCLUDED.generation",
        "astra_signature_replay_v108",
        "astra_rollout_authorization_v108",
        "astra_receipt_authorization_v108",
        "reserve_receipt_authorization",
        "RECEIPT_AUTHORIZATION_RESERVED",
        "ROLLBACK",
    ):
        if token == "ROLLBACK":
            if "connection.rollback()" not in repository:
                findings.append("repository_boundary:rollback")
        elif token not in repository:
            findings.append(f"repository_boundary:{token}")

    migration = read("migrations/v108/001_asymmetric_signing_authority.sql")
    for token in (
        "nonce text NOT NULL UNIQUE",
        "command_digest text NOT NULL UNIQUE",
        "astra_signing_event_append_only_v108",
        "astra_receipt_authorization_v108",
        "executor_signature_id text NOT NULL UNIQUE",
        "FOREIGN KEY (authorization_bundle_digest, command_digest)",
        "BEFORE UPDATE OR DELETE",
        "REVOKE ALL",
    ):
        if token not in migration:
            findings.append(f"migration_boundary:{token}")

    return {
        "schema": 108,
        "status": "PASS" if not findings else "FAIL",
        "required_files": len(REQUIRED),
        "findings": findings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-architecture-audit-v108")
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = audit(args.root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
