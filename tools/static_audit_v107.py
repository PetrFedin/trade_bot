from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

FORBIDDEN = (
    "allow_redirects=True",
    "tls_verify=False",
    'method="POST"',
    'method="PUT"',
    'method="DELETE"',
    "subprocess.",
    "pickle.loads",
    "eval(",
    "exec(",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    candidates = sorted((root / "app" / "runtime").glob("*v107.py")) + sorted((root / "tools").glob("*v107.py"))
    for path in candidates:
        if path.name == "static_audit_v107.py":
            continue
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                findings.append(f"{path.relative_to(root)}:{token}")

    adapter_path = root / "app/runtime/kubernetes_rollout_adapter_v107.py"
    if adapter_path.is_file():
        adapter = adapter_path.read_text(encoding="utf-8")
        if adapter.count('method="PATCH"') != 1:
            findings.append("kubernetes_adapter:unexpected_patch_surface")
        if adapter.count('method="GET"') != 1:
            findings.append("kubernetes_adapter:unexpected_get_surface")
        if 'if method not in {"GET", "PATCH"}' not in adapter:
            findings.append("kubernetes_adapter:method_allowlist_missing")

    service_path = root / "app/runtime/rollout_service_v107.py"
    if service_path.is_file():
        service = service_path.read_text(encoding="utf-8")
        if "apply_patch_once" not in service or service.count("apply_patch_once") != 1:
            findings.append("rollout_service:mutation_surface_not_single")
        if "RECOVERY_MUTATION_ALLOWED_V107 = False" not in service:
            findings.append("rollout_service:recovery_boundary_missing")

    status_path = root / "LIVE_EXECUTION_STATUS_V107.json"
    if status_path.is_file():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        for key in ("external_order_routing_allowed", "live_trading_allowed"):
            if status.get(key) is not False:
                findings.append(f"status_boundary:{key}")
        if status.get("kubernetes_rollout_actuator_verified") is not False:
            findings.append("status_boundary:kubernetes_rollout_actuator_verified")

    return {
        "schema": 107,
        "status": "PASS" if not findings else "FAIL",
        "files_scanned": len(candidates),
        "findings": findings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-static-audit-v107")
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = audit(args.root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
