from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

FORBIDDEN = (
    "allow_redirects=True",
    "tls_verify=False",
    "method=\"POST\"",
    "method=\"PATCH\"",
    "method=\"DELETE\"",
    "subprocess.",
    "pickle.loads",
    "eval(",
    "exec(",
)


def audit(root: Path) -> dict[str, object]:
    findings: list[str] = []
    candidates = list((root / "app" / "runtime").glob("*v106.py")) + list((root / "tools").glob("*v106.py"))
    for path in candidates:
        if path.name.startswith("static_audit_"):
            continue
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                findings.append(f"{path.relative_to(root)}:{token}")
    adapter = (root / "app/runtime/kubernetes_qualification_adapter_v106.py").read_text(encoding="utf-8")
    if adapter.count("method=\"GET\"") != 1:
        findings.append("kubernetes_adapter:unexpected_method_surface")
    status = json.loads((root / "LIVE_EXECUTION_STATUS_V106.json").read_text(encoding="utf-8"))
    for key in ("kubernetes_mutations_allowed", "external_order_routing_allowed", "live_trading_allowed"):
        if status.get(key) is not False:
            findings.append(f"status_boundary:{key}")
    return {"schema": 106, "status": "PASS" if not findings else "FAIL", "files_scanned": len(candidates), "findings": findings}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    result = audit(args.root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
