from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

FORBIDDEN_RUNTIME = (
    "Ed25519PrivateKey",
    "private_key_bytes",
    "private_key_b64",
    "hmac.new",
    "pickle.loads",
    "subprocess.",
    "eval(",
    "exec(",
    "allow_redirects=True",
    "tls_verify=False",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    runtime_files = sorted((root / "app" / "runtime").glob("*v108.py"))
    for path in runtime_files:
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_RUNTIME:
            if token in text:
                findings.append(f"{path.relative_to(root)}:{token}")

    authority_path = root / "app/runtime/signing_authority_v108.py"
    if authority_path.is_file():
        authority = authority_path.read_text(encoding="utf-8")
        if authority.count("Ed25519PublicKey") < 3:
            findings.append("authority:ed25519_verification_surface_missing")
        if "MappingProxyType" not in authority:
            findings.append("authority:verified_keyring_not_immutable")
        if "distinct owners" not in authority:
            findings.append("authority:separation_of_duties_missing")

    repository_path = root / "app/runtime/postgres_signing_repository_v108.py"
    if repository_path.is_file():
        repository = repository_path.read_text(encoding="utf-8")
        if repository.count("INSERT INTO astra_signature_replay_v108") != 1:
            findings.append("repository:unexpected_replay_write_surface")
        if "connection.rollback()" not in repository:
            findings.append("repository:transaction_rollback_missing")

    status_path = root / "LIVE_EXECUTION_STATUS_V108.json"
    if status_path.is_file():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        for key in (
            "production_signing_authority_verified",
            "production_kubernetes_mutation_authorized",
            "external_order_routing_allowed",
            "live_trading_allowed",
        ):
            if status.get(key) is not False:
                findings.append(f"status_boundary:{key}")

    return {
        "schema": 108,
        "status": "PASS" if not findings else "FAIL",
        "files_scanned": len(runtime_files),
        "findings": findings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-static-audit-v108")
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = audit(args.root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
