from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

FORBIDDEN = (
    "ssl._create_unverified_context",
    "ssl.CERT_NONE",
    "verify=False",
    "paper_order_writes_enabled: bool = True",
    "live_trading_allowed: bool = True",
    "external_order_routing_allowed: bool = True",
)


def audit(root: Path) -> tuple[str, ...]:
    failures: list[str] = []
    runtime = root / "app/runtime/alpaca_paper_adapter_v100.py"
    text = runtime.read_text(encoding="utf-8")
    for token in FORBIDDEN:
        if token in text:
            failures.append(f"forbidden:{token}")
    if "ASTRA_ALPACA_PAPER_SECRET_KEY" not in text:
        failures.append("credential-env-boundary-missing")
    migration = (root / "migrations/v100/001_alpaca_paper_sandbox.sql").read_text(
        encoding="utf-8"
    ).lower()
    if "secret" in migration or "api_key" in migration:
        failures.append("migration-must-not-store-secrets")
    if "revoke all" not in migration:
        failures.append("migration-public-rights-not-revoked")
    return tuple(failures)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    failures = audit(args.root.resolve())
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        return 1
    print("PASS schema100 static audit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
