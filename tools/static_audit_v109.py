from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

FORBIDDEN_RUNTIME = (
    "Ed25519PrivateKey",
    "private_key_bytes",
    "private_key_b64",
    "hmac.new",
    "pickle.loads",
    "subprocess.",
    "ssl._create_unverified_context",
    "ssl.CERT_NONE",
    "check_hostname = False",
    "http://",
    "time.sleep(",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    runtime_files = sorted((root / "app" / "runtime").glob("*_v109.py"))
    for path in runtime_files:
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_RUNTIME:
            if token in text:
                findings.append(f"{path.relative_to(root)}:{token}")

    remote_path = root / "app/runtime/remote_signer_attestation_v109.py"
    if remote_path.is_file():
        remote = remote_path.read_text(encoding="utf-8")
        for token, finding in (
            ("context.verify_mode != ssl.CERT_REQUIRED", "tls_peer_verification_missing"),
            ("context.check_hostname is not True", "tls_hostname_verification_missing"),
            ("context.minimum_version < ssl.TLSVersion.TLSv1_3", "tls13_minimum_missing"),
            ("class _NoRedirectV109", "redirect_block_missing"),
            ('self._request("POST", "/v1/signing/requests", body)', "single_post_surface_missing"),
            ('self._request("GET", f"/v1/signing/requests/', "get_reconciliation_missing"),
            ("self._repository.mark_dispatch_started(", "durable_dispatch_boundary_missing"),
            ("RemoteSignerUncertainErrorV109", "ambiguous_outcome_boundary_missing"),
        ):
            if token not in remote:
                findings.append(f"remote_signer:{finding}")
        if remote.count('self._request("POST", "/v1/signing/requests", body)') != 1:
            findings.append("remote_signer:post_surface_not_unique")

    repository_path = root / "app/runtime/postgres_remote_signer_repository_v109.py"
    if repository_path.is_file():
        repository = repository_path.read_text(encoding="utf-8")
        if "connection.rollback()" not in repository:
            findings.append("repository:transaction_rollback_missing")
        if "audit checkpoint compare-and-set rejected" not in repository:
            findings.append("repository:checkpoint_cas_missing")

    status_path = root / "LIVE_EXECUTION_STATUS_V109.json"
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
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
        "files_scanned": len(runtime_files),
        "findings": findings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-static-audit-v109")
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = audit(args.root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
