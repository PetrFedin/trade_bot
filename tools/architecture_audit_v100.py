from __future__ import annotations

import argparse
from pathlib import Path
import re
from typing import Sequence

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


def _successor_identity_is_valid(pyproject: str) -> tuple[bool, bool]:
    name_match = re.search(
        r'^name\s*=\s*"astra-schema(?P<schema>\d+)[^"]*"\s*$',
        pyproject,
        flags=re.MULTILINE,
    )
    version_match = re.search(
        r'^version\s*=\s*"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"\s*$',
        pyproject,
        flags=re.MULTILINE,
    )
    identity_valid = bool(name_match and int(name_match.group("schema")) >= 100)
    version_valid = bool(
        version_match
        and tuple(int(version_match.group(part)) for part in ("major", "minor", "patch"))
        >= (7, 30, 0)
    )
    return identity_valid, version_valid


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
    identity_valid, version_valid = _successor_identity_is_valid(pyproject)
    if not identity_valid:
        failures.append("package-identity")
    if not version_valid:
        failures.append("package-version")
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
