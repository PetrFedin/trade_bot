from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from tools.product_identity import stable_identity_findings

REQUIRED = (
    "app/runtime/alpaca_paper_adapter_v100.py",
    "tests/test_alpaca_paper_adapter_v100.py",
    "migrations/v100/001_alpaca_paper_sandbox.sql",
    "tools/platform_v100.py",
    "tools/static_audit_v100.py",
    ".github/workflows/schema100-alpaca-paper-sandbox.yml",
    "ENGINEERING_REPORT_V100.md",
    "RELEASE_NOTES_V100.md",
    "LIVE_EXECUTION_STATUS_V100.json",
)


def audit(root: Path) -> tuple[str, ...]:
    failures: list[str] = []
    for relative in REQUIRED:
        if not (root / relative).is_file():
            failures.append(f"missing:{relative}")
    runtime = (root / "app/runtime/alpaca_paper_adapter_v100.py").read_text(
        encoding="utf-8"
    )
    for required in (
        "https://paper-api.alpaca.markets",
        "wss://paper-api.alpaca.markets/stream",
        "paper_order_writes_enabled: bool = False",
        "maximum_read_attempts",
        "StaleStreamGeneration",
        "FILLED_QUANTITY_REGRESSION",
        "BROKER_TIME_REGRESSION",
    ):
        if required not in runtime:
            failures.append(f"runtime-control-missing:{required}")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    failures.extend(
        finding.replace("_", "-")
        for finding in stable_identity_findings(pyproject, minimum_version=(7, 30, 0))
    )
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
    print("PASS schema100 architecture audit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
